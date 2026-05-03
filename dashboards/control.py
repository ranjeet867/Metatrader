"""
control.py — Streamlit dashboard wrapping the v2 reconciliation-enforced backtester.

NON-NEGOTIABLE: every backtest displayed here goes through core.backtest.run_backtest.
The dashboard is a VIEW + CONTROL layer; it never re-computes PnL or equity.
The reconciliation invariant is shown PROMINENTLY at the top of every result.

Layout:
  - Sidebar: ticker / tf / strategy + auto-built params form + friction inputs
  - Tab 1: Backtest (equity, drawdown, trade tape, reasons histogram)
  - Tab 2: Data refresh (re-fetch parquets via MT5 bridge)
  - Tab 3: Sweep (multi-select grid → heatmap)
  - Tab 4: Paper (UI stub; not wired to live execution yet)

Run with:  make dashboard   (or  .venv/bin/streamlit run dashboards/control.py)
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# After path insertion, import v2 internals — we go through these and ONLY these
# for any backtest math.
from core.backtest import partition_train_test, run_backtest  # noqa: E402
from core.data import fetch_from_bridge, load_parquet, save_parquet  # noqa: E402


# ---------------------------------------------------------------------------
# Strategy discovery
# ---------------------------------------------------------------------------

def discover_strategies() -> dict[str, tuple[type, type | None]]:
    """Walk strategies/ and return {strategy_name: (StrategyClass, ParamsClass)}.

    A strategy is detected by:
      - lives in a module under strategies/
      - has a ``name`` class attribute
      - has a ``signals(self, candles)`` method
      - is paired with a frozen dataclass whose name ends in "Params"
    """
    out: dict[str, tuple[type, type | None]] = {}
    strat_dir = ROOT / "strategies"
    for path in sorted(strat_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        modname = f"strategies.{path.stem}"
        try:
            mod = importlib.import_module(modname)
        except Exception as e:
            st.warning(f"Could not import {modname}: {e}")
            continue

        strat_cls: type | None = None
        params_cls: type | None = None
        for clsname, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod.__name__:
                continue
            if dataclasses.is_dataclass(cls) and clsname.endswith("Params"):
                params_cls = cls
            elif (hasattr(cls, "name")
                  and isinstance(cls.name, str)
                  and hasattr(cls, "signals")
                  and not dataclasses.is_dataclass(cls)):
                strat_cls = cls

        if strat_cls is not None:
            out[strat_cls.name] = (strat_cls, params_cls)
    return out


# ---------------------------------------------------------------------------
# Data discovery
# ---------------------------------------------------------------------------

def discover_data() -> dict[str, dict[str, Path]]:
    """Walk data/ and return {ticker: {tf: parquet_path}}."""
    out: dict[str, dict[str, Path]] = {}
    data_dir = ROOT / "data"
    for path in sorted(data_dir.glob("*.parquet")):
        stem = path.stem
        if "_" not in stem:
            continue
        # Last underscore separates tf from ticker (ticker may itself contain '.')
        ticker, tf = stem.rsplit("_", 1)
        out.setdefault(ticker, {})[tf] = path
    return out


# Per-ticker money_per_unit_USD_per_lot lookup (mirrors scripts/sweep_grid.py).
# Used as defaults; user can override in the sidebar.
DEFAULT_MONEY_PER_UNIT: dict[str, float] = {
    "US100.cash": 1.0,
    "US500.cash": 1.0,
    "GER40.cash": 1.0,
    "EU50.cash":  1.10,
    "EURUSD":     100_000.0,
    "GBPUSD":     100_000.0,
    "AUDUSD":     100_000.0,
    "NZDUSD":     100_000.0,
    "USDJPY":     700.0,
    "GBPJPY":     700.0,
}
DEFAULT_LOTS: dict[str, float] = {
    "US100.cash": 6.5,
    "US500.cash": 20.0,
    "GER40.cash": 3.0,
    "EU50.cash":  20.0,
    "EURUSD":     1.0,
    "GBPUSD":     1.0,
    "AUDUSD":     1.5,
    "NZDUSD":     1.5,
    "USDJPY":     1.0,
    "GBPJPY":     0.7,
}


# ---------------------------------------------------------------------------
# Data freshness
# ---------------------------------------------------------------------------

def freshness_summary(data_index: dict[str, dict[str, Path]]
                       ) -> tuple[list[dict], int, int, int]:
    """Compute per-file age in days and bucket counts.

    Returns (rows, n_green, n_yellow, n_red).
      rows: list of dicts with ticker, tf, modified_utc, age_days, bucket
      bucket ∈ {"green", "yellow", "red"}: <2 days, 2-7 days, >7 days
    """
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for ticker, tfs in sorted(data_index.items()):
        for tf, path in sorted(tfs.items()):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            age_days = (now - mtime).total_seconds() / 86400.0
            if age_days < 2:
                bucket = "green"
            elif age_days < 7:
                bucket = "yellow"
            else:
                bucket = "red"
            rows.append({
                "ticker": ticker, "tf": tf,
                "modified_utc": mtime.isoformat(timespec="seconds"),
                "age_days": round(age_days, 2), "bucket": bucket,
            })
    n_green = sum(1 for r in rows if r["bucket"] == "green")
    n_yellow = sum(1 for r in rows if r["bucket"] == "yellow")
    n_red = sum(1 for r in rows if r["bucket"] == "red")
    return rows, n_green, n_yellow, n_red


def render_freshness_bar(data_index: dict[str, dict[str, Path]]) -> None:
    """Top-of-page status bar showing how stale each cached parquet is.

    Always rendered; cheap to compute. Click the expander for the per-file table.
    """
    if not data_index:
        st.warning("No cached parquets — use the Data Refresh tab to fetch some.")
        return
    rows, ng, ny, nr = freshness_summary(data_index)

    # Compact summary row
    cols = st.columns([1, 1, 1, 2])
    cols[0].markdown(
        f"<span style='background:#1c8a4a;color:white;padding:4px 10px;border-radius:4px;'>"
        f"🟢  fresh &lt;2d: <b>{ng}</b></span>",
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        f"<span style='background:#b08800;color:white;padding:4px 10px;border-radius:4px;'>"
        f"🟡  2–7d: <b>{ny}</b></span>",
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        f"<span style='background:#aa1a1a;color:white;padding:4px 10px;border-radius:4px;'>"
        f"🔴  stale &gt;7d: <b>{nr}</b></span>",
        unsafe_allow_html=True,
    )
    if nr > 0:
        cols[3].markdown(
            "🔴 **Some data is older than a week.** Run "
            "`make refresh-data` (or use the Data Refresh tab) before "
            "trusting backtest results."
        )
    elif ny > 0:
        cols[3].markdown("🟡 Some data is 2-7 days old; refresh recommended for daily revalidation.")
    else:
        cols[3].markdown("🟢 All cached data is current (<2 days).")

    with st.expander("Per-file ages", expanded=False):
        df = pd.DataFrame(rows)
        # Add a coloured emoji marker column for readability
        emoji_map = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
        df["age"] = df["bucket"].map(emoji_map) + " " + df["age_days"].astype(str) + "d"
        st.dataframe(df[["ticker", "tf", "modified_utc", "age"]],
                      use_container_width=True,
                      height=min(360, 36 * (len(rows) + 1)))


# ---------------------------------------------------------------------------
# Auto-form for a Params dataclass
# ---------------------------------------------------------------------------

def render_params_form(params_cls: type, key_prefix: str) -> Any:
    """Render st.number_input / st.checkbox widgets for each field of a frozen
    dataclass and return an instance of that dataclass with the user's values.
    """
    field_values: dict[str, Any] = {}
    for f in dataclasses.fields(params_cls):
        widget_key = f"{key_prefix}__{f.name}"
        default = f.default
        ftype = f.type
        # f.type may be a string when `from __future__ import annotations` is set.
        # Resolve it against the dataclass's module if possible.
        resolved = ftype
        if isinstance(ftype, str):
            type_ns = {**vars(sys.modules[params_cls.__module__]),
                        "int": int, "float": float, "bool": bool, "str": str}
            try:
                resolved = eval(ftype, type_ns)
            except Exception:
                resolved = type(default) if default is not dataclasses.MISSING else str

        label = f.name.replace("_", " ")
        if resolved is bool:
            field_values[f.name] = st.checkbox(
                label, value=bool(default), key=widget_key
            )
        elif resolved is int:
            field_values[f.name] = int(st.number_input(
                label, value=int(default), step=1, key=widget_key,
            ))
        elif resolved is float:
            # Step proportional to value magnitude
            step = max(abs(float(default)) * 0.1, 0.01) if default else 0.1
            field_values[f.name] = float(st.number_input(
                label, value=float(default), step=step,
                format="%.4f", key=widget_key,
            ))
        else:
            field_values[f.name] = st.text_input(
                label, value=str(default), key=widget_key
            )
    return params_cls(**field_values)


# ---------------------------------------------------------------------------
# Backtest invocation — THE one place we call run_backtest()
# ---------------------------------------------------------------------------

def run_one(df: pd.DataFrame,
             strategy_instance: Any,
             *,
             starting_balance: float,
             lots: float,
             money_per_unit: float,
             commission_per_trade: float,
             slippage_atr_frac: float,
             train_pct: float,
             ) -> dict[str, Any]:
    """Run a single backtest through core.backtest.run_backtest and partition.
    Returns a flat dict of everything the dashboard needs for display."""
    sigs = strategy_instance.signals(df)
    result = run_backtest(
        df, sigs,
        starting_balance=starting_balance,
        lots=lots,
        money_per_unit_price=money_per_unit,
        commission_per_trade=commission_per_trade,
        slippage_per_fill_atr_frac=slippage_atr_frac,
    )
    train, test = partition_train_test(result, train_pct, n_bars=len(df))
    return {
        "result": result,
        "signals": sigs,
        "train": train,
        "test": test,
        "split_idx": int(len(df) * train_pct),
        "candles": df,
    }


# ---------------------------------------------------------------------------
# Plotly helpers
# ---------------------------------------------------------------------------

def equity_figure(eq: pd.DataFrame, split_time: pd.Timestamp,
                   starting_balance: float, title: str) -> go.Figure:
    """Equity curve with the train/test boundary marked."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["time"], y=eq["equity"],
        mode="lines", name="equity",
        line=dict(color="#0a4", width=1.4),
    ))
    fig.add_hline(y=starting_balance, line=dict(color="#888", dash="dash"),
                   annotation_text=f"start ${starting_balance:,.0f}",
                   annotation_position="bottom right")
    if split_time is not None:
        # Use add_shape directly (plotly's add_vline with annotation does Timestamp
        # arithmetic that breaks on tz-aware pandas Timestamps).
        fig.add_shape(type="line",
                       x0=split_time, x1=split_time, xref="x",
                       y0=0, y1=1, yref="paper",
                       line=dict(color="#c33", dash="dot"))
        fig.add_annotation(
            x=split_time, y=1.0, xref="x", yref="paper",
            text="train / test split", showarrow=False,
            xanchor="left", yanchor="top",
            font=dict(color="#c33", size=10),
        )
    fig.update_layout(
        title=title, height=360, margin=dict(l=10, r=10, t=40, b=10),
        xaxis_title="time", yaxis_title="equity ($)",
        hovermode="x unified",
    )
    return fig


def drawdown_figure(eq: pd.DataFrame) -> go.Figure:
    """Drawdown filled-area chart from running peak."""
    if eq.empty:
        return go.Figure()
    peak = eq["equity"].cummax()
    dd_pct = (eq["equity"] - peak) / peak * 100.0
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["time"], y=dd_pct,
        mode="lines", name="drawdown %",
        line=dict(color="#c33", width=0.8),
        fill="tozeroy", fillcolor="rgba(204,51,51,0.25)",
    ))
    fig.update_layout(
        height=180, margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="time", yaxis_title="drawdown %",
        hovermode="x unified",
    )
    return fig


def trade_reasons_figure(trades) -> go.Figure:
    """Histogram of close_reason values."""
    if not trades:
        return go.Figure().update_layout(height=200,
                                          title="(no trades to bin)")
    c = Counter(t.close_reason for t in trades)
    keys = list(c.keys())
    vals = [c[k] for k in keys]
    colors = {"target": "#0a4", "stop": "#c33",
              "time": "#aa0", "end_of_data": "#888"}
    fig = go.Figure([go.Bar(
        x=keys, y=vals,
        marker_color=[colors.get(k, "#666") for k in keys],
    )])
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10),
                       title="Close reasons",
                       xaxis_title="reason", yaxis_title="trades")
    return fig


def trades_to_dataframe(trades, candles: pd.DataFrame) -> pd.DataFrame:
    """Turn ClosedTrade list into a sortable DataFrame for st.dataframe."""
    rows = []
    for t in trades:
        rows.append({
            "direction":   t.direction,
            "entry_time":  candles["time"].iloc[t.entry_bar_idx],
            "exit_time":   candles["time"].iloc[t.exit_bar_idx],
            "entry":       round(t.entry_price, 5),
            "exit":        round(t.exit_price, 5),
            "stop":        round(t.stop_price, 5),
            "target":      round(t.target_price, 5),
            "lots":        t.lots,
            "pnl_$":       round(t.realized_pnl, 2),
            "R":           round(t.r_multiple, 3),
            "reason":      t.close_reason,
            "bars_held":   t.exit_bar_idx - t.entry_bar_idx,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------

def reconcile_badge(reconciles: bool, sum_pnl: float, eq_pnl: float,
                     tolerance: float) -> None:
    if reconciles:
        st.success(
            f"✅  Reconciled  |  sum_realized_pnl = ${sum_pnl:+,.4f}  "
            f"|  equity_curve_pnl = ${eq_pnl:+,.4f}  "
            f"|  diff < ${tolerance}"
        )
    else:
        st.error(
            f"⛔  RECONCILIATION FAILED  |  sum = ${sum_pnl:+,.4f}  "
            f"|  eq = ${eq_pnl:+,.4f}  "
            f"|  diff = ${sum_pnl - eq_pnl:+,.4f}  >  tol ${tolerance}\n\n"
            "Do not trust any number on this page until this is fixed.",
            icon="⛔",
        )


def render_backtest_tab(strategies, data_index) -> None:
    if not data_index:
        st.warning("No parquets found under data/. Use the Data Refresh tab "
                    "to fetch some, then reload.")
        return
    if not strategies:
        st.warning("No strategies found under strategies/.")
        return

    # ---- Sidebar (within an expander to keep the tab UI tight) ----
    with st.sidebar:
        st.header("⚙️ Run config")
        ticker = st.selectbox("ticker", sorted(data_index.keys()), key="bt_ticker")
        tfs_avail = sorted(data_index[ticker].keys(),
                            key=lambda x: {"M15": 0, "H1": 1, "H4": 2, "D1": 3}.get(x, 9))
        tf = st.selectbox("timeframe", tfs_avail, key="bt_tf")

        strat_name = st.selectbox("strategy", sorted(strategies.keys()),
                                    key="bt_strat")
        StratCls, ParamsCls = strategies[strat_name]

        st.markdown("**Strategy params**")
        if ParamsCls is None:
            st.write("(no params dataclass discovered — using defaults)")
            params_obj = None
        else:
            params_obj = render_params_form(ParamsCls, key_prefix=f"bt_p_{strat_name}")

        st.markdown("**Friction & sizing**")
        balance = float(st.number_input("starting balance ($)", value=91_400.0,
                                         step=1000.0, key="bt_bal"))
        default_lots = DEFAULT_LOTS.get(ticker, 0.1)
        default_mpu = DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0)
        lots = float(st.number_input("lots", value=default_lots,
                                      step=0.1, format="%.2f", key="bt_lots"))
        mpu = float(st.number_input("money per 1.0 unit per lot ($)",
                                     value=default_mpu, step=1.0, format="%.2f",
                                     key="bt_mpu"))
        comm = float(st.number_input("commission $/trade", value=3.0,
                                      step=0.5, format="%.2f", key="bt_comm"))
        slip = float(st.number_input("slippage (× ATR)", value=0.10,
                                      step=0.05, format="%.3f", key="bt_slip"))
        train_pct = float(st.slider("train fraction (rest is OOS)",
                                       min_value=0.1, max_value=0.95,
                                       value=0.6, step=0.05, key="bt_train"))

        run_btn = st.button("▶  Run Backtest", type="primary",
                             use_container_width=True, key="bt_run")

    # ---- Main area ----
    st.subheader(f"Backtest — {ticker} {tf} • {strat_name}")
    if not run_btn and "bt_last" not in st.session_state:
        st.info("Set parameters in the sidebar and click **Run Backtest**.")
        return
    if run_btn:
        try:
            df = load_parquet(data_index[ticker][tf])
        except Exception as e:
            st.error(f"Could not load parquet: {e}")
            return
        if params_obj is None:
            try:
                strat = StratCls()
            except Exception as e:
                st.error(f"Cannot instantiate strategy without params: {e}")
                return
        else:
            strat = StratCls(params_obj)
        try:
            run = run_one(
                df, strat,
                starting_balance=balance, lots=lots, money_per_unit=mpu,
                commission_per_trade=comm, slippage_atr_frac=slip,
                train_pct=train_pct,
            )
        except Exception as e:
            st.error(f"Backtest failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc())
            return
        st.session_state["bt_last"] = run
        st.session_state["bt_meta"] = {
            "ticker": ticker, "tf": tf, "strat": strat_name,
            "balance": balance, "tolerance": run["result"].reconcile_tolerance,
        }

    run = st.session_state["bt_last"]
    meta = st.session_state["bt_meta"]
    r = run["result"]

    # ---- Reconciliation badge (always shown at top) ----
    reconcile_badge(r.reconciles, r.sum_realized_pnl, r.equity_curve_pnl,
                     meta["tolerance"])

    # ---- Headline metrics ----
    n = r.n_trades
    if n > 0:
        wins = sum(1 for t in r.trades if t.realized_pnl > 0)
        win_rate = wins / n * 100.0
        gw = sum(t.realized_pnl for t in r.trades if t.realized_pnl > 0)
        gl = -sum(t.realized_pnl for t in r.trades if t.realized_pnl <= 0)
        pf = (gw / gl) if gl > 0 else float("inf") if gw > 0 else 0.0
        avg_R = sum(t.r_multiple for t in r.trades) / n
    else:
        win_rate = pf = avg_R = 0.0
    ret_pct = r.equity_curve_pnl / meta["balance"] * 100.0 if meta["balance"] else 0.0

    cols = st.columns(5)
    cols[0].metric("trades", f"{n}")
    cols[1].metric("win rate", f"{win_rate:.1f}%")
    pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
    cols[2].metric("profit factor", pf_text)
    cols[3].metric("avg R", f"{avg_R:+.3f}")
    cols[4].metric("return", f"{ret_pct:+.2f}%",
                    delta=f"${r.equity_curve_pnl:+,.0f}")

    # ---- Train/Test partition ----
    train, test = run["train"], run["test"]
    st.markdown("**Train / Test partition (first 60% / last 40% by entry bar):**")
    pcol1, pcol2 = st.columns(2)
    for col, m in [(pcol1, train), (pcol2, test)]:
        pf_t = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        col.markdown(
            f"**{m.label.upper()}** &nbsp;&nbsp; "
            f"n={m.n_trades}  PF={pf_t}  avg R={m.avg_R:+.3f}  "
            f"win%={m.win_rate:.1f}  $={m.sum_pnl:+,.2f}",
            unsafe_allow_html=True,
        )

    # ---- Equity + drawdown ----
    df = run["candles"]
    split_time = df["time"].iloc[run["split_idx"]] if 0 < run["split_idx"] < len(df) else None
    title = f"{meta['ticker']} {meta['tf']} • {meta['strat']} — equity"
    st.plotly_chart(equity_figure(r.equity_curve, split_time,
                                    meta["balance"], title),
                     use_container_width=True)
    st.plotly_chart(drawdown_figure(r.equity_curve),
                     use_container_width=True)

    # ---- Trade tape + reasons ----
    if n > 0:
        c1, c2 = st.columns([3, 1])
        with c1:
            st.markdown("**Trade tape** (sortable)")
            st.dataframe(trades_to_dataframe(r.trades, df),
                          use_container_width=True, height=360)
        with c2:
            st.plotly_chart(trade_reasons_figure(r.trades),
                             use_container_width=True)
    else:
        st.info("No trades produced — try different params or a longer history.")


def render_refresh_tab(data_index) -> None:
    st.subheader("Data Refresh — fetch fresh candles via MT5 Bridge")

    ticker = st.text_input("ticker (must match broker symbol exactly)",
                            value="US100.cash", key="rf_ticker")
    tf = st.selectbox("timeframe", ["M15", "H1", "H4", "D1"],
                       index=1, key="rf_tf")
    n_bars = int(st.number_input("number of bars to fetch",
                                   value=8000, step=500, min_value=200,
                                   max_value=100_000, key="rf_n"))

    # Existing-files panel
    with st.expander("Currently cached parquets", expanded=True):
        rows = []
        for t, tfs in sorted(data_index.items()):
            for tname, p in sorted(tfs.items()):
                stat = p.stat()
                rows.append({
                    "ticker": t, "tf": tname,
                    "size_kb": round(stat.st_size / 1024, 1),
                    "modified_utc": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds"),
                })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True,
                          height=min(360, 36 * (len(rows) + 1)))
        else:
            st.caption("(no parquets yet)")

    fetch_btn = st.button("⬇  Refresh from MT5 Bridge", type="primary",
                            key="rf_run")
    if fetch_btn:
        out_path = ROOT / "data" / f"{ticker}_{tf}.parquet"
        prog = st.progress(0.0, text="Sending request to bridge...")
        try:
            df = fetch_from_bridge(ticker, tf, n_bars)
            prog.progress(0.7, text=f"Validating {len(df)} bars...")
            save_parquet(df, out_path)
            prog.progress(1.0, text="Done.")
            st.success(
                f"✓ Fetched {len(df):,} bars  "
                f"first={df['time'].iloc[0]}  last={df['time'].iloc[-1]}\n\n"
                f"Saved → {out_path.relative_to(ROOT)}"
            )
        except TimeoutError as e:
            prog.empty()
            st.error(
                f"Bridge timed out: {e}\n\n"
                "Check that the MT5 EA is running on the Wine VM and polling "
                "the req/ directory."
            )
        except Exception as e:
            prog.empty()
            st.error(f"Fetch failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc())


def render_sweep_tab(strategies, data_index) -> None:
    st.subheader("Sweep — test a grid of (ticker × tf × strategy) cells")

    if not data_index:
        st.warning("No data parquets cached. Use the Data Refresh tab first.")
        return

    tickers = st.multiselect("tickers", sorted(data_index.keys()),
                              default=sorted(data_index.keys())[:3],
                              key="sw_tickers")
    all_tfs = sorted({tf for tfs in data_index.values() for tf in tfs.keys()})
    tfs = st.multiselect("timeframes", all_tfs, default=all_tfs,
                          key="sw_tfs")
    snames = st.multiselect("strategies", sorted(strategies.keys()),
                             default=sorted(strategies.keys()),
                             key="sw_strats")

    col1, col2, col3 = st.columns(3)
    comm = float(col1.number_input("commission $/trade", value=3.0, step=0.5,
                                     format="%.2f", key="sw_comm"))
    slip = float(col2.number_input("slippage × ATR", value=0.10, step=0.05,
                                     format="%.3f", key="sw_slip"))
    train_pct = float(col3.slider("train fraction", 0.1, 0.95, 0.6, 0.05,
                                     key="sw_train"))

    col1, col2, col3 = st.columns(3)
    min_n = int(col1.number_input("min n_test", value=20, step=5, key="sw_minn"))
    min_pf = float(col2.number_input("min test_PF", value=1.20, step=0.05,
                                       format="%.2f", key="sw_minpf"))
    min_R = float(col3.number_input("min test_R", value=0.10, step=0.05,
                                      format="%.3f", key="sw_minr"))

    run_btn = st.button("▶  Run sweep", type="primary", key="sw_run")
    if not run_btn:
        st.caption("Click **Run sweep** to evaluate the selected cells.")
        return
    if not (tickers and tfs and snames):
        st.warning("Select at least one ticker, timeframe, and strategy.")
        return

    rows = []
    failed_reconcile = []
    total = len(tickers) * len(tfs) * len(snames)
    prog = st.progress(0.0, text="Running sweep...")
    done = 0
    for ticker in tickers:
        mpu = DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0)
        lots = DEFAULT_LOTS.get(ticker, 0.1)
        for tf in tfs:
            if tf not in data_index.get(ticker, {}):
                done += len(snames)
                prog.progress(done / total, text=f"skipping {ticker} {tf} (no data)")
                continue
            try:
                df = load_parquet(data_index[ticker][tf])
            except Exception as e:
                done += len(snames)
                prog.progress(done / total, text=f"skip {ticker} {tf}: {e}")
                continue
            for sname in snames:
                done += 1
                prog.progress(done / total, text=f"{ticker} {tf} {sname}")
                StratCls, ParamsCls = strategies[sname]
                try:
                    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
                except Exception:
                    continue
                try:
                    sigs = strat.signals(df)
                    r = run_backtest(
                        df, sigs, starting_balance=91_400, lots=lots,
                        money_per_unit_price=mpu,
                        commission_per_trade=comm,
                        slippage_per_fill_atr_frac=slip,
                    )
                except Exception:
                    continue
                if not r.reconciles:
                    failed_reconcile.append(f"{ticker}/{tf}/{sname}")
                    continue
                train, test = partition_train_test(r, train_pct, n_bars=len(df))
                rows.append({
                    "ticker": ticker, "tf": tf, "strategy": sname,
                    "n_train": train.n_trades, "n_test": test.n_trades,
                    "train_PF": train.profit_factor, "test_PF": test.profit_factor,
                    "train_R": train.avg_R, "test_R": test.avg_R,
                    "test_ret_pct": test.sum_pnl / 91_400 * 100.0,
                })
    prog.empty()

    if failed_reconcile:
        st.error(
            f"⛔ {len(failed_reconcile)} cells failed reconciliation:\n\n"
            + "\n".join(f"- {f}" for f in failed_reconcile[:20])
        )

    if not rows:
        st.warning("No cells produced results.")
        return

    df_grid = pd.DataFrame(rows)
    df_grid["test_PF"] = df_grid["test_PF"].replace(float("inf"), 9.99)
    df_grid["train_PF"] = df_grid["train_PF"].replace(float("inf"), 9.99)
    df_grid_sorted = df_grid.sort_values("test_R", ascending=False)
    st.markdown(f"**{len(df_grid)} cells**  —  sorted by test_R desc")
    st.dataframe(df_grid_sorted, use_container_width=True, height=380)

    # ---- Heatmap (ticker × strategy, color = test_R) ----
    if len(df_grid) >= 2:
        # If multiple TFs are present, average across them in the heatmap; if
        # only one TF, show as-is. Either way, pivot ticker × strategy.
        if df_grid["tf"].nunique() > 1:
            st.caption("Heatmap aggregates across selected timeframes (mean test_R).")
            hm = df_grid.groupby(["ticker", "strategy"])["test_R"].mean().reset_index()
        else:
            hm = df_grid[["ticker", "strategy", "test_R"]]
        pivot = hm.pivot(index="ticker", columns="strategy", values="test_R")
        fig = go.Figure(data=go.Heatmap(
            z=pivot.values, x=pivot.columns, y=pivot.index,
            colorscale="RdYlGn", zmid=0,
            colorbar=dict(title="test_R"),
            text=[[f"{v:+.2f}" if pd.notna(v) else "" for v in row]
                   for row in pivot.values],
            texttemplate="%{text}", textfont=dict(size=10),
        ))
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10),
                            title="test_R heatmap (out-of-sample R per trade)")
        st.plotly_chart(fig, use_container_width=True)

    # ---- Survivors filter ----
    survivors = df_grid_sorted[
        (df_grid_sorted["n_test"] >= min_n)
        & (df_grid_sorted["test_PF"] >= min_pf)
        & (df_grid_sorted["test_R"] >= min_R)
        & (df_grid_sorted["train_R"] > 0)
        & (df_grid_sorted["train_PF"] >= 1.0)
    ]
    st.markdown(f"### ⭐ Survivors  ({len(survivors)})")
    if len(survivors):
        st.dataframe(survivors, use_container_width=True,
                      height=min(360, 36 * (len(survivors) + 1)))
    else:
        st.info("No cells met the acceptance criteria.")


def render_paper_tab(strategies, data_index) -> None:
    st.subheader("Paper Trade — UI stub  (not wired to live execution)")
    st.warning(
        "⚠️  This tab does NOT send orders to the broker. It writes a state "
        "file under `data/paper_state/` and prints what an order would look like. "
        "Live execution requires the MT5-parity-verified executor module — "
        "which doesn't exist yet."
    )

    if not data_index or not strategies:
        st.info("No tickers or strategies available.")
        return

    ticker = st.selectbox("ticker", sorted(data_index.keys()), key="pp_ticker")
    sname = st.selectbox("strategy", sorted(strategies.keys()), key="pp_strat")
    StratCls, ParamsCls = strategies[sname]
    lots = float(st.number_input("lots", value=DEFAULT_LOTS.get(ticker, 0.1),
                                   step=0.1, format="%.2f", key="pp_lots"))

    state_dir = ROOT / "data" / "paper_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{sname}__{ticker}.json"

    if state_file.exists():
        st.markdown("**Current state file:**")
        st.code(state_file.read_text(), language="json")

    cstart, cstop = st.columns(2)
    if cstart.button("▶  Start Paper Loop", key="pp_start", type="primary"):
        # Read latest cached candle to resolve a "would-open" price
        tfs = sorted(data_index[ticker].keys(),
                      key=lambda x: {"M15": 0, "H1": 1, "H4": 2, "D1": 3}.get(x, 9))
        tf = tfs[0]
        try:
            df = load_parquet(data_index[ticker][tf])
            try:
                strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
            except Exception as e:
                st.error(f"Cannot instantiate {sname}: {e}")
                return
            sigs = strat.signals(df)
            latest_sig = sigs[-1] if sigs else None
            payload = {
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker, "tf": tf, "strategy": sname, "lots": lots,
                "live": False,
                "comment": "STUB — would open at price below if this were live.",
                "latest_signal": (None if latest_sig is None else {
                    "bar_idx": latest_sig.bar_idx,
                    "direction": latest_sig.direction,
                    "entry_price": latest_sig.entry_price,
                    "stop_price": latest_sig.stop_price,
                    "target_price": latest_sig.target_price,
                    "max_hold_bars": latest_sig.max_hold_bars,
                    "reason": latest_sig.reason,
                }),
            }
            state_file.write_text(json.dumps(payload, indent=2, default=str))
            st.success(f"Paper loop initialised: {state_file.name}")
            if latest_sig is not None:
                st.info(
                    f"📋 STUB: would open {latest_sig.direction} {ticker} "
                    f"{lots} lots at ${latest_sig.entry_price:,.4f} "
                    f"stop=${latest_sig.stop_price:,.4f} "
                    f"target=${latest_sig.target_price:,.4f} "
                    f"(no order actually sent)"
                )
            else:
                st.info("No latest signal — strategy did not fire on most recent bar.")
        except Exception as e:
            st.error(f"Failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc())

    if cstop.button("■  Clear paper state", key="pp_stop"):
        if state_file.exists():
            state_file.unlink()
            st.info(f"Cleared {state_file.name}")
        else:
            st.caption("(nothing to clear)")

    st.markdown("---")
    st.markdown(
        "**TODO:** wire to a real executor. This requires:\n"
        "1. `core/executor.py` (not yet implemented)\n"
        "2. MT5 Strategy Tester parity verified for the chosen strategy "
        "(±10% trade count, ±10% total P&L)\n"
        "3. A guarded order-submission path with kill-switch\n"
        "4. End-to-end reconciliation between paper-engine and v2 backtester output\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="v2 Quant Control", layout="wide",
                        initial_sidebar_state="expanded")
    st.title("📈  mt5_quant_trader_v2 — Control Dashboard")
    st.caption(
        "Every result on this page goes through `core.backtest.run_backtest` — "
        "the same reconciliation-enforced engine the test suite verifies. "
        "If you ever see the ⛔ red banner, do NOT trust the numbers."
    )

    strategies = discover_strategies()
    data_index = discover_data()

    # Data-freshness status bar — always rendered at the top of the page.
    render_freshness_bar(data_index)
    st.markdown("---")

    tab_bt, tab_data, tab_sweep, tab_paper = st.tabs(
        ["🔬  Backtest", "🔄  Data Refresh", "🧮  Sweep", "📝  Paper (stub)"]
    )
    with tab_bt:
        render_backtest_tab(strategies, data_index)
    with tab_data:
        render_refresh_tab(data_index)
    with tab_sweep:
        render_sweep_tab(strategies, data_index)
    with tab_paper:
        render_paper_tab(strategies, data_index)


if __name__ == "__main__":
    main()
