"""
1_📊_Backtest.py — single-strategy backtest with reconciliation enforced
+ in-process sweep + train/test partition + Promote buttons.

Migrated from dashboards/control.py with the following NEW behaviour:
  - Time-guard toggles (weekend_flat / daily_close_flat)
  - Reconciliation banner shows the EXACT $ divergence
  - "Promote to Paper Portfolio" — adds the config to session_state for Page 3
  - Sweep mode is in-process (no subprocess), with st.progress
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.config import load_config   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.symbol_info_loader import try_load as try_load_symbol_info   # noqa: E402
from dashboards.components import reconciliation_badge   # noqa: E402
from dashboards.components.charts import (   # noqa: E402
    drawdown_figure,
    equity_figure,
    heatmap_test_R,
    trade_reasons_figure,
    trades_to_dataframe,
)
from dashboards.components.forms import render_params_form   # noqa: E402
from dashboards.components.state import (   # noqa: E402
    DEFAULT_LOTS,
    DEFAULT_MONEY_PER_UNIT,
    KEY_LAST_BACKTEST,
    KEY_LAST_BACKTEST_META,
    KEY_PROMOTE_TO_PAPER,
    discover_data,
    discover_strategies,
)


def _run_one(df, strat, *, balance, lots, mpu, comm, slip, train_pct,
              symbol, enforce_weekend_flat, enforce_daily_flat,
              no_entry_min, risk_pct=None, symbol_info=None):
    sigs = strat.signals(df)
    result = run_backtest(
        df, sigs,
        starting_balance=balance, lots=lots, money_per_unit_price=mpu,
        commission_per_trade=comm,
        slippage_per_fill_atr_frac=slip,
        symbol=symbol,
        enforce_weekend_flat=enforce_weekend_flat,
        enforce_daily_flat=enforce_daily_flat,
        no_entry_minutes_before_close=no_entry_min,
        risk_pct=risk_pct, symbol_info=symbol_info,
    )
    train, test = partition_train_test(result, train_pct, n_bars=len(df))
    return {"result": result, "signals": sigs,
            "train": train, "test": test,
            "split_idx": int(len(df) * train_pct),
            "candles": df}


def render_backtest_section(strategies, data_index, cfg):
    if not data_index:
        st.warning("No parquets cached. Use the Data Manager page to fetch some.")
        return
    if not strategies:
        st.warning("No strategies found under strategies/.")
        return

    # ----- Sidebar form -----
    with st.sidebar:
        st.markdown("### ⚙️  Backtest config")
        ticker = st.selectbox("ticker", sorted(data_index.keys()), key="bt_ticker")
        tfs_avail = sorted(data_index[ticker].keys(),
                            key=lambda x: {"M15": 0, "H1": 1, "H4": 2, "D1": 3}.get(x, 9))
        tf = st.selectbox("timeframe", tfs_avail, key="bt_tf")

        strat_name = st.selectbox("strategy", sorted(strategies.keys()),
                                    key="bt_strat")
        StratCls, ParamsCls = strategies[strat_name]

        st.markdown("**Params**")
        if ParamsCls is None:
            st.write("(no params dataclass — using defaults)")
            params_obj = None
        else:
            params_obj = render_params_form(ParamsCls, key_prefix=f"bt_p_{strat_name}")

        st.markdown("**Friction & sizing**")
        balance = float(st.number_input("starting balance ($)", value=91_400.0,
                                          step=1000.0, key="bt_bal"))

        # Sizing mode toggle: dynamic risk_pct (default) vs fixed lots
        sizing_mode = st.radio(
            "sizing mode",
            ["risk %", "fixed lots"],
            index=0, horizontal=True, key="bt_sizing_mode",
            help="risk %: lots computed per-trade from equity × risk%. "
                  "fixed lots: legacy mode (every trade same size).",
        )
        if sizing_mode == "risk %":
            risk_pct_val = float(st.number_input(
                "risk per trade (%)",
                value=0.3, step=0.1, format="%.2f", min_value=0.05, max_value=5.0,
                key="bt_risk_pct",
            ))
            lots = 0.0   # unused
            sym_info = try_load_symbol_info(ticker)
            if sym_info is None:
                st.warning(f"No symbol_info entry for `{ticker}` — falling back "
                            "to fixed lots. Run `make refresh-symbol-info`.")
                sizing_mode = "fixed lots"
                risk_pct_val = None
                sym_info = None
        else:
            risk_pct_val = None
            sym_info = None
            lots = float(st.number_input("lots", value=DEFAULT_LOTS.get(ticker, 0.1),
                                            step=0.1, format="%.2f", key="bt_lots"))

        mpu = float(st.number_input("money per 1.0 unit per lot ($)",
                                      value=DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0),
                                      step=1.0, format="%.2f", key="bt_mpu"))
        comm = float(st.number_input("commission $/trade", value=3.0,
                                       step=0.5, format="%.2f", key="bt_comm"))
        slip = float(st.number_input("slippage (× ATR)", value=0.10,
                                       step=0.05, format="%.3f", key="bt_slip"))
        train_pct = float(st.slider("train fraction (rest is OOS)",
                                       0.1, 0.95, 0.6, 0.05, key="bt_train"))

        st.markdown("**Time guards (INVARIANT-8)**")
        enforce_weekend = st.checkbox("Enforce weekend flat",
                                        value=cfg.weekend_flat_all,
                                        key="bt_weekend_flat",
                                        help="Force-close ALL positions at "
                                             "Friday 19:55 UTC.")
        enforce_daily = st.checkbox("Enforce daily flat (stocks/indices)",
                                      value=True, key="bt_daily_flat",
                                      help="Force-close stocks/indices at "
                                           "weekday 19:55 UTC. FX/metals exempt.")
        no_entry = int(st.number_input("no-entry window (min before close)",
                                          value=0, step=5, min_value=0,
                                          max_value=120, key="bt_no_entry"))

        run_btn = st.button("▶  Run Backtest", type="primary",
                              use_container_width=True, key="bt_run")

    # ----- Main area -----
    st.subheader(f"Backtest — {ticker} {tf} • {strat_name}")
    if not run_btn and KEY_LAST_BACKTEST not in st.session_state:
        st.info("Set parameters in the sidebar and click **Run Backtest**.")
        return

    if run_btn:
        try:
            df = load_parquet(data_index[ticker][tf])
        except Exception as e:
            st.error(f"Could not load parquet: {e}")
            return
        try:
            strat = StratCls() if params_obj is None else StratCls(params_obj)
        except Exception as e:
            st.error(f"Could not instantiate strategy: {e}")
            return
        try:
            run = _run_one(
                df, strat,
                balance=balance, lots=lots, mpu=mpu,
                comm=comm, slip=slip, train_pct=train_pct,
                symbol=ticker,
                enforce_weekend_flat=enforce_weekend,
                enforce_daily_flat=enforce_daily,
                no_entry_min=no_entry,
                risk_pct=risk_pct_val, symbol_info=sym_info,
            )
        except Exception as e:
            st.error(f"Backtest failed: {e}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc())
            return
        st.session_state[KEY_LAST_BACKTEST] = run
        st.session_state[KEY_LAST_BACKTEST_META] = {
            "ticker": ticker, "tf": tf, "strat": strat_name,
            "balance": balance, "lots": lots, "mpu": mpu,
            "comm": comm, "slip": slip, "train_pct": train_pct,
            "enforce_weekend": enforce_weekend, "enforce_daily": enforce_daily,
            "no_entry_minutes": no_entry,
            "params_obj": params_obj,
            "tolerance": run["result"].reconcile_tolerance,
            "sizing_mode": sizing_mode, "risk_pct": risk_pct_val,
        }

    run = st.session_state[KEY_LAST_BACKTEST]
    meta = st.session_state[KEY_LAST_BACKTEST_META]
    r = run["result"]

    # Reconciliation banner — INVARIANT-1
    reconciliation_badge.render(r.reconciles, r.sum_realized_pnl,
                                 r.equity_curve_pnl, meta["tolerance"])

    # Headline metrics
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

    cols = st.columns(6)
    cols[0].metric("trades", f"{n}")
    cols[1].metric("win rate", f"{win_rate:.1f}%")
    pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
    cols[2].metric("profit factor", pf_text)
    cols[3].metric("avg R", f"{avg_R:+.3f}")
    cols[4].metric("return", f"{ret_pct:+.2f}%",
                    delta=f"${r.equity_curve_pnl:+,.0f}")
    cols[5].metric("skipped", f"{r.skipped_signals}",
                    help="signals dropped because in_no_entry_window")

    # Sizing summary
    if meta.get("sizing_mode") == "risk %":
        if n > 0:
            avg_lots = sum(t.lots for t in r.trades) / n
            avg_risk_dollars = sum(t.initial_dollar_risk for t in r.trades) / n
            avg_risk_pct = avg_risk_dollars / meta["balance"] * 100 if meta["balance"] else 0
            st.caption(
                f"📐 **Dynamic sizing** target {meta['risk_pct']:.2f}% per trade "
                f"→ avg lots = {avg_lots:.2f}, "
                f"avg $ at risk = ${avg_risk_dollars:,.0f} "
                f"({avg_risk_pct:.2f}% of starting balance)."
            )
        else:
            st.caption(f"📐 Dynamic sizing target {meta['risk_pct']:.2f}% per trade.")
    else:
        st.caption(f"📐 Fixed sizing: {meta.get('lots', 0)} lots per trade.")

    # Train/Test
    train, test = run["train"], run["test"]
    st.markdown("**Train / Test partition (by entry bar):**")
    pcol1, pcol2 = st.columns(2)
    for col, m in [(pcol1, train), (pcol2, test)]:
        pf_t = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        col.markdown(
            f"**{m.label.upper()}** &nbsp; n={m.n_trades} &nbsp; "
            f"PF={pf_t} &nbsp; avg R={m.avg_R:+.3f} &nbsp; "
            f"win%={m.win_rate:.1f} &nbsp; $={m.sum_pnl:+,.2f}",
            unsafe_allow_html=True,
        )

    # Equity + drawdown
    df = run["candles"]
    split_time = df["time"].iloc[run["split_idx"]] if 0 < run["split_idx"] < len(df) else None
    title = f"{meta['ticker']} {meta['tf']} • {meta['strat']} — equity"
    st.plotly_chart(equity_figure(r.equity_curve, split_time,
                                    meta["balance"], title),
                     use_container_width=True)
    st.plotly_chart(drawdown_figure(r.equity_curve),
                     use_container_width=True)

    # Trade tape + reasons
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

    # Promote buttons
    st.markdown("---")
    pcol1, pcol2 = st.columns(2)
    if pcol1.button("📤  Promote to Strategy Studio", type="secondary",
                      use_container_width=True, key="bt_promote_studio"):
        st.session_state["_studio_compare_left"] = {
            "ticker": meta["ticker"], "tf": meta["tf"], "strat": meta["strat"],
            "params": meta.get("params_obj"),
        }
        st.success("Pushed to Strategy Studio (Page 2 → 'Side A' slot).")

    if pcol2.button("📡  Promote to Paper Portfolio", type="secondary",
                      use_container_width=True, key="bt_promote_paper"):
        portfolio = st.session_state.get(KEY_PROMOTE_TO_PAPER, [])
        portfolio.append({
            "ticker": meta["ticker"], "tf": meta["tf"], "strat": meta["strat"],
            "lots": meta["lots"], "mpu": meta["mpu"],
            "params": meta.get("params_obj"),
            "enforce_weekend": meta["enforce_weekend"],
            "enforce_daily": meta["enforce_daily"],
        })
        st.session_state[KEY_PROMOTE_TO_PAPER] = portfolio
        st.success(f"Added to paper portfolio ({len(portfolio)} entries). "
                    "Open Page 3 → Tab B to start.")


def render_sweep_section(strategies, data_index, cfg):
    """In-process sweep with st.progress — no subprocess."""
    st.markdown("---")
    st.markdown("### 🧮  Sweep (multi-cell evaluation)")

    if not data_index:
        return

    tickers = st.multiselect("tickers", sorted(data_index.keys()),
                              default=sorted(data_index.keys())[:3],
                              key="sw_tickers")
    all_tfs = sorted({tf for tfs in data_index.values() for tf in tfs.keys()})
    tfs = st.multiselect("timeframes", all_tfs, default=["D1"],
                          key="sw_tfs")
    snames = st.multiselect("strategies", sorted(strategies.keys()),
                              default=sorted(strategies.keys())[:3],
                              key="sw_strats")

    cols = st.columns(4)
    comm = float(cols[0].number_input("comm $/trade", value=3.0, step=0.5,
                                         format="%.2f", key="sw_comm"))
    slip = float(cols[1].number_input("slip × ATR", value=0.10, step=0.05,
                                         format="%.3f", key="sw_slip"))
    train_pct = float(cols[2].slider("train frac", 0.1, 0.95, 0.6, 0.05,
                                        key="sw_train"))
    enforce_flats = cols[3].checkbox("Enforce time guards", value=True,
                                       key="sw_flats")

    if not st.button("▶  Run sweep", type="primary", key="sw_run"):
        return
    if not (tickers and tfs and snames):
        st.warning("Select at least one ticker, tf, and strategy.")
        return

    rows = []
    failed = []
    total = len(tickers) * len(tfs) * len(snames)
    prog = st.progress(0.0, text="Running sweep...")
    done = 0
    for ticker in tickers:
        mpu = DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0)
        lots_t = DEFAULT_LOTS.get(ticker, 0.1)
        for tf in tfs:
            if tf not in data_index.get(ticker, {}):
                done += len(snames)
                prog.progress(done / total, text=f"skip {ticker} {tf}")
                continue
            try:
                df = load_parquet(data_index[ticker][tf])
            except Exception:
                done += len(snames)
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
                        df, sigs, starting_balance=91_400, lots=lots_t,
                        money_per_unit_price=mpu,
                        commission_per_trade=comm,
                        slippage_per_fill_atr_frac=slip,
                        symbol=ticker,
                        enforce_weekend_flat=enforce_flats and cfg.weekend_flat_all,
                        enforce_daily_flat=enforce_flats,
                    )
                except Exception:
                    continue
                if not r.reconciles:
                    failed.append(f"{ticker}/{tf}/{sname} div=${r.reconcile_divergence:+.2f}")
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
    if failed:
        st.error("⛔  Cells that failed reconciliation (divergence shown):\n\n"
                  + "\n".join(f"- {f}" for f in failed[:20]))
    if not rows:
        st.warning("No cells produced results.")
        return
    df_grid = pd.DataFrame(rows).sort_values("test_R", ascending=False)
    df_grid["test_PF"] = df_grid["test_PF"].replace(float("inf"), 9.99)
    df_grid["train_PF"] = df_grid["train_PF"].replace(float("inf"), 9.99)
    st.markdown(f"**{len(df_grid)} cells** sorted by test_R")
    st.dataframe(df_grid, use_container_width=True, height=380)
    if len(df_grid) >= 2:
        st.plotly_chart(heatmap_test_R(df_grid), use_container_width=True)


def main():
    st.set_page_config(page_title="Backtest", page_icon="📊", layout="wide")
    st.title("📊  Backtest")
    st.caption("Single-strategy reconciliation-enforced backtest. Time guards "
                "are opt-in here; a published 'survivor' should reconcile WITH "
                "guards on so paper/live behaves the same.")
    cfg = load_config()
    strats = discover_strategies()
    data = discover_data()
    render_backtest_section(strats, data, cfg)
    render_sweep_section(strats, data, cfg)


if __name__ == "__main__":
    main()
else:
    main()
