"""
2_🔬_Strategy_Studio.py — A/B comparison + single-param sweep + walk-forward.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.config import load_config   # noqa: E402
from core.data import load_parquet   # noqa: E402
from dashboards.components import reconciliation_badge   # noqa: E402
from dashboards.components.charts import equity_figure   # noqa: E402
from dashboards.components.forms import render_params_form   # noqa: E402
from dashboards.components.state import (   # noqa: E402
    DEFAULT_LOTS,
    DEFAULT_MONEY_PER_UNIT,
    discover_data,
    discover_strategies,
)


def _backtest(df, strat, *, balance, lots, mpu, comm, slip, symbol,
               enforce_weekend, enforce_daily):
    sigs = strat.signals(df)
    return run_backtest(
        df, sigs, starting_balance=balance, lots=lots,
        money_per_unit_price=mpu,
        commission_per_trade=comm,
        slippage_per_fill_atr_frac=slip,
        symbol=symbol,
        enforce_weekend_flat=enforce_weekend,
        enforce_daily_flat=enforce_daily,
    )


def render_compare(strategies, data_index, side: str, label: str, cfg):
    with st.container(border=True):
        st.markdown(f"### Side **{label}**")
        ticker = st.selectbox("ticker", sorted(data_index.keys()),
                                key=f"st_{side}_ticker")
        tfs = sorted(data_index[ticker].keys(),
                      key=lambda x: {"M15": 0, "H1": 1, "D1": 3}.get(x, 9))
        tf = st.selectbox("tf", tfs, key=f"st_{side}_tf")
        sname = st.selectbox("strategy", sorted(strategies.keys()),
                                key=f"st_{side}_s")
        StratCls, ParamsCls = strategies[sname]
        params_obj = (render_params_form(ParamsCls, key_prefix=f"st_{side}_p")
                       if ParamsCls is not None else None)
        comm = float(st.number_input("comm", value=3.0, key=f"st_{side}_c"))
        slip = float(st.number_input("slip", value=0.10, format="%.3f",
                                        key=f"st_{side}_sl"))
        enforce_d = st.checkbox("daily flat", value=True, key=f"st_{side}_ed")
        enforce_w = st.checkbox("weekend flat", value=True, key=f"st_{side}_ew")

        if not st.button(f"▶  Run {label}", key=f"st_{side}_run",
                          width="stretch"):
            return None
        try:
            df = load_parquet(data_index[ticker][tf])
            strat = StratCls() if params_obj is None else StratCls(params_obj)
            r = _backtest(df, strat,
                           balance=91_400, lots=DEFAULT_LOTS.get(ticker, 0.1),
                           mpu=resolve_money_per_unit(ticker),
                           comm=comm, slip=slip, symbol=ticker,
                           enforce_weekend=enforce_w, enforce_daily=enforce_d)
        except Exception as e:
            st.error(f"failed: {e}")
            with st.expander("traceback"):
                st.code(traceback.format_exc())
            return None
        reconciliation_badge.render(r.reconciles, r.sum_realized_pnl,
                                     r.equity_curve_pnl, r.reconcile_tolerance)
        train, test = partition_train_test(r, 0.6, n_bars=len(df))
        cols = st.columns(3)
        cols[0].metric("trades", r.n_trades)
        cols[1].metric("sum P&L", f"${r.sum_realized_pnl:+,.2f}")
        pf = "inf" if test.profit_factor == float("inf") else f"{test.profit_factor:.2f}"
        cols[2].metric("test PF", pf)
        st.plotly_chart(equity_figure(r.equity_curve, None, 91_400,
                                        f"{label}: {ticker} {tf} {sname}"),
                         width="stretch")
        return {"result": r, "ticker": ticker, "tf": tf, "strat": sname}


def render_param_sweep(strategies, data_index):
    st.markdown("---")
    st.markdown("### 🎚️  Single-param sweep")
    ticker = st.selectbox("ticker", sorted(data_index.keys()), key="ps_ticker")
    tf = st.selectbox("tf", sorted(data_index[ticker].keys()), key="ps_tf")
    sname = st.selectbox("strategy", sorted(strategies.keys()), key="ps_s")
    StratCls, ParamsCls = strategies[sname]
    if ParamsCls is None:
        st.info("This strategy has no Params dataclass — sweep N/A.")
        return
    import dataclasses
    pfields = [f for f in dataclasses.fields(ParamsCls)
                if isinstance(f.default, (int, float))
                and not isinstance(f.default, bool)]
    if not pfields:
        st.info("No numeric param to sweep.")
        return
    pname = st.selectbox("param", [f.name for f in pfields], key="ps_param")
    field = next(f for f in pfields if f.name == pname)
    cols = st.columns(3)
    lo = float(cols[0].number_input("min", value=float(field.default) * 0.5,
                                       key="ps_lo"))
    hi = float(cols[1].number_input("max", value=float(field.default) * 1.5,
                                       key="ps_hi"))
    n_steps = int(cols[2].number_input("steps", value=10, step=1,
                                          min_value=3, max_value=40,
                                          key="ps_steps"))
    if not st.button("▶  Run sweep", key="ps_run"):
        return
    df = load_parquet(data_index[ticker][tf])
    values = np.linspace(lo, hi, n_steps)
    rows = []
    prog = st.progress(0.0, text="sweeping...")
    for i, v in enumerate(values):
        v_typed = int(v) if isinstance(field.default, int) else float(v)
        kwargs = {pname: v_typed}
        try:
            params = ParamsCls(**kwargs)
            strat = StratCls(params)
            r = run_backtest(
                df, strat.signals(df), starting_balance=91_400,
                lots=DEFAULT_LOTS.get(ticker, 0.1),
                money_per_unit_price=resolve_money_per_unit(ticker),
                commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
                symbol=ticker, enforce_daily_flat=True,
            )
            train, test = partition_train_test(r, 0.6, n_bars=len(df))
            pf = test.profit_factor if test.profit_factor != float("inf") else 9.99
            rows.append({pname: v_typed, "n_test": test.n_trades,
                         "test_PF": pf, "test_R": test.avg_R})
        except Exception:
            pass
        prog.progress((i + 1) / n_steps)
    prog.empty()
    if not rows:
        st.warning("All sweep cells failed.")
        return
    df_s = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_s[pname], y=df_s["test_PF"],
                              name="test_PF", mode="lines+markers"))
    fig.add_trace(go.Scatter(x=df_s[pname], y=df_s["test_R"],
                              name="test_R", mode="lines+markers", yaxis="y2"))
    fig.update_layout(
        title=f"{sname} • {ticker} {tf} — sweep on `{pname}`",
        xaxis_title=pname,
        yaxis=dict(title="test_PF", side="left"),
        yaxis2=dict(title="test_R", overlaying="y", side="right"),
        height=380, margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, width="stretch")
    st.dataframe(df_s, width="stretch")


def render_walk_forward(strategies, data_index):
    st.markdown("---")
    st.markdown("### ↔️  Walk-forward stability")
    ticker = st.selectbox("ticker", sorted(data_index.keys()), key="wf_ticker")
    tf = st.selectbox("tf", sorted(data_index[ticker].keys()), key="wf_tf")
    sname = st.selectbox("strategy", sorted(strategies.keys()), key="wf_s")
    StratCls, ParamsCls = strategies[sname]
    cols = st.columns(2)
    train_bars = int(cols[0].number_input("train window (bars)", value=200,
                                              step=50, key="wf_tw"))
    test_bars = int(cols[1].number_input("test window (bars)", value=100,
                                             step=25, key="wf_te"))
    if not st.button("▶  Run walk-forward", key="wf_run"):
        return
    try:
        df = load_parquet(data_index[ticker][tf])
        strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    except Exception as e:
        st.error(f"setup failed: {e}")
        return
    n = len(df)
    rows = []
    start = 0
    while start + train_bars + test_bars <= n:
        sub = df.iloc[start: start + train_bars + test_bars].reset_index(drop=True)
        try:
            r = run_backtest(
                sub, strat.signals(sub), starting_balance=91_400,
                lots=DEFAULT_LOTS.get(ticker, 0.1),
                money_per_unit_price=resolve_money_per_unit(ticker),
                commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
                symbol=ticker, enforce_daily_flat=True,
            )
            split_pct = train_bars / (train_bars + test_bars)
            train, test = partition_train_test(r, split_pct,
                                                n_bars=len(sub))
            rows.append({
                "window_start_idx": start,
                "window_start_time": sub["time"].iloc[0],
                "test_n": test.n_trades,
                "test_PF": (9.99 if test.profit_factor == float("inf")
                            else test.profit_factor),
                "test_R": test.avg_R,
            })
        except Exception:
            pass
        start += test_bars   # roll forward by test_bars
    if not rows:
        st.warning("No windows.")
        return
    df_w = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_w["window_start_time"], y=df_w["test_PF"],
                              mode="lines+markers", name="test_PF"))
    fig.add_trace(go.Scatter(x=df_w["window_start_time"], y=df_w["test_R"],
                              mode="lines+markers", name="test_R", yaxis="y2"))
    fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"))
    fig.update_layout(
        title=f"Walk-forward {sname} • {ticker} {tf}",
        xaxis_title="window start",
        yaxis=dict(title="test_PF"),
        yaxis2=dict(title="test_R", overlaying="y", side="right"),
        height=380, margin=dict(l=10, r=10, t=40, b=10),
    )
    st.plotly_chart(fig, width="stretch")
    st.dataframe(df_w, width="stretch")


def render_new_strategy_wizard():
    """Phase 24 — generate a new strategy file from a template."""
    st.markdown("---")
    st.markdown("### ✨  New Strategy wizard")
    cols = st.columns([1, 1, 2])
    category = cols[0].selectbox(
        "category",
        ["Trend", "Mean-rev", "Breakout", "Time-of-day", "Pullback", "Custom"],
        key="ns_cat",
    )
    tf_default = cols[1].selectbox("default tf", ["M5", "M15", "H1", "H4", "D1"],
                                      index=2, key="ns_tf")
    name = cols[2].text_input("strategy_name (snake_case, unique)", value="",
                                 key="ns_name").strip().lower()
    if st.button("⚙️  Generate skeleton", type="primary", key="ns_gen"):
        if not name or not all(c.isalnum() or c == "_" for c in name):
            st.error("Name must be snake_case (letters, digits, underscore).")
            return
        out = REPO / "strategies" / f"{name}.py"
        if out.exists():
            st.error(f"`strategies/{name}.py` already exists.")
            return
        out.write_text(_strategy_template(name, category, tf_default))
        st.success(f"Created `strategies/{name}.py`")
        st.code(out.read_text(), language="python")
        st.info("Run `make screen STRATEGY=" + name + "` once you've filled in `signals()`.")


def _strategy_template(name: str, category: str, tf: str) -> str:
    cls_name = "".join(p.title() for p in name.split("_"))
    return f'''"""
{name}.py — generated by Strategy Studio wizard.
Category: {category}  •  Default tf: {tf}

TODO: implement `signals()` to return your entry list. Each Signal must
satisfy:
  LONG : stop < entry < target
  SHORT: target < entry < stop

Then run:  make screen STRATEGY={name}
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder, ema  # add others as needed
from core.strategy import Signal


@dataclass(frozen=True)
class {cls_name}Params:
    # TODO: define your params with sensible defaults
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 2.0
    long_only: bool = False


class {cls_name}:
    name = "{name}"

    def __init__(self, params: {cls_name}Params = {cls_name}Params()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < p.atr_period + 2:
            return []

        atr = atr_wilder(candles, p.atr_period).to_numpy()
        c = candles["close"].to_numpy()

        out: list[Signal] = []
        for i in range(p.atr_period + 1, n):
            a = atr[i]
            if a <= 0:
                continue
            # TODO: replace with real entry logic for the {category} category
            # Example placeholder: never fires
            should_long = False
            should_short = False

            if should_long:
                stop = c[i] - p.stop_atr_mult * a
                target = c[i] + p.target_atr_mult * a
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=float(c[i]),
                    stop_price=float(stop),
                    target_price=float(target),
                    reason="{name}_long",
                ))
            elif should_short and not p.long_only:
                stop = c[i] + p.stop_atr_mult * a
                target = c[i] - p.target_atr_mult * a
                out.append(Signal(
                    bar_idx=i, direction="SHORT",
                    entry_price=float(c[i]),
                    stop_price=float(stop),
                    target_price=float(target),
                    reason="{name}_short",
                ))
        return out
'''


def main():
    st.set_page_config(page_title="Strategy Studio", page_icon="🔬", layout="wide")
    st.title("🔬  Strategy Studio")
    cfg = load_config()
    strats = discover_strategies()
    data_index = discover_data()
    st.markdown("### A/B comparison")
    c1, c2 = st.columns(2)
    with c1:
        render_compare(strats, data_index, "a", "A", cfg)
    with c2:
        render_compare(strats, data_index, "b", "B", cfg)
    render_param_sweep(strats, data_index)
    render_walk_forward(strats, data_index)
    render_new_strategy_wizard()


main()
