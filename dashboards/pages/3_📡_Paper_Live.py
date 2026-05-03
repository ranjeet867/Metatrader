"""
3_📡_Paper_Live.py — Replay (Tab A) | Paper Portfolio (Tab B) | Live (Tab C)
                  + MT5 Tester Parity (Tab D)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import storage   # noqa: E402
from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.config import load_config, save_config   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.parity_gate import ParityGate   # noqa: E402
from core.replay import replay_run   # noqa: E402
from core.symbol_info_loader import try_load as try_load_symbol_info   # noqa: E402
from core.time_guards import time_guard_cfg_from_risk_config   # noqa: E402
from dashboards.components import reconciliation_badge   # noqa: E402
from dashboards.components.charts import equity_figure   # noqa: E402
from dashboards.components.forms import render_params_form   # noqa: E402
from dashboards.components.state import (   # noqa: E402
    DEFAULT_LOTS,
    DEFAULT_MONEY_PER_UNIT,
    KEY_FTMO_RISK_ACCEPTED,
    KEY_OVERRIDE_PARITY_RECENCY,
    KEY_PROMOTE_TO_PAPER,
    discover_data,
    discover_strategies,
)


# ---------------------------------------------------------------------------
# Paper-loop registry — held across reruns by st.cache_resource
# ---------------------------------------------------------------------------

@st.cache_resource
def _paper_runs() -> dict:
    """{run_id: PaperLoop} — a session-wide registry."""
    return {}


# ---------------------------------------------------------------------------
# Tab A — Replay
# ---------------------------------------------------------------------------

def render_replay_tab(strategies, data_index, cfg):
    st.subheader("🎬  Replay — proves the engine reproduces backtest")
    cols = st.columns([1, 1, 1, 1, 1])
    ticker = cols[0].selectbox("ticker", sorted(data_index.keys()),
                                  key="rp_ticker")
    tf = cols[1].selectbox("tf", sorted(data_index[ticker].keys()), key="rp_tf")
    sname = cols[2].selectbox("strategy", sorted(strategies.keys()),
                                 key="rp_s")
    risk_pct_val = float(cols[3].number_input("risk per trade %",
                                                  value=0.3, step=0.1,
                                                  format="%.2f",
                                                  min_value=0.05, max_value=5.0,
                                                  key="rp_risk"))
    enforce_flats = cols[4].checkbox("Enforce time guards", value=True,
                                        key="rp_flats")
    StratCls, ParamsCls = strategies[sname]
    params_obj = (render_params_form(ParamsCls, key_prefix="rp_p")
                   if ParamsCls is not None else None)
    if not st.button("▶  Run Replay", type="primary", key="rp_run"):
        return

    try:
        df = load_parquet(data_index[ticker][tf])
        strat = StratCls() if params_obj is None else StratCls(params_obj)
    except Exception as e:
        st.error(f"setup: {e}")
        return

    sym_info = try_load_symbol_info(ticker)
    use_dynamic = sym_info is not None
    if not use_dynamic:
        st.warning(
            f"No symbol_info for `{ticker}` — replay will use fixed lots from "
            "DEFAULT_LOTS table. Run `make refresh-symbol-info`."
        )

    # Both backtest AND replay — so we can show parity numerically
    bt = run_backtest(
        df, strat.signals(df), starting_balance=91_400,
        lots=DEFAULT_LOTS.get(ticker, 0.1),
        money_per_unit_price=DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        symbol=ticker,
        enforce_weekend_flat=enforce_flats and cfg.weekend_flat_all,
        enforce_daily_flat=enforce_flats,
        risk_pct=risk_pct_val if use_dynamic else None,
        symbol_info=sym_info,
    )
    tg_cfg = time_guard_cfg_from_risk_config(cfg) if enforce_flats else None
    rp = replay_run(
        df, strat,
        symbol=ticker, tf=tf,
        starting_balance=91_400,
        lots=DEFAULT_LOTS.get(ticker, 0.1),
        money_per_unit_price=DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        time_guard_cfg=tg_cfg,
        risk_pct=risk_pct_val if use_dynamic else None,
        symbol_info=sym_info,
    )

    # Reconciliation badges for BOTH
    st.markdown("**Backtest reconciliation:**")
    reconciliation_badge.render(bt.reconciles, bt.sum_realized_pnl,
                                 bt.equity_curve_pnl, bt.reconcile_tolerance)
    st.markdown("**Replay reconciliation:**")
    reconciliation_badge.render(rp.reconciles, rp.sum_realized_pnl,
                                 rp.equity_curve_pnl, rp.reconcile_tolerance)

    # Parity check: replay PnL must equal backtest PnL within $0.01
    div = abs(rp.sum_realized_pnl - bt.sum_realized_pnl)
    if div < 0.01:
        st.success(
            f"✅  **Replay-parity PASSED** — divergence vs backtest = ${div:.4f}"
        )
        # Record to parity_log
        ParityGate(REPO / "data" / "v2.db").record_pass(
            sname, divergence_dollars=div,
        )
    else:
        st.error(
            f"⛔  **REPLAY-PARITY FAILED** — divergence = ${div:.2f}\n\n"
            "Live trading of this strategy is BLOCKED until the bug is "
            "root-caused and fixed."
        )

    cols = st.columns(4)
    cols[0].metric("backtest trades", bt.n_trades)
    cols[1].metric("replay trades", rp.n_trades,
                    delta=rp.n_trades - bt.n_trades)
    cols[2].metric("backtest $", f"${bt.sum_realized_pnl:+,.2f}")
    cols[3].metric("replay $", f"${rp.sum_realized_pnl:+,.2f}",
                    delta=f"${div:.4f} div")

    if use_dynamic and rp.n_trades > 0:
        avg_lots = sum(t.lots for t in rp.trades) / rp.n_trades
        avg_risk_d = sum(t.initial_dollar_risk for t in rp.trades) / rp.n_trades
        st.caption(
            f"📐 Sized at {risk_pct_val:.2f}% per trade → "
            f"avg lots = {avg_lots:.2f}, avg $ at risk = ${avg_risk_d:,.0f}"
        )

    st.plotly_chart(equity_figure(rp.equity_curve, None, 91_400,
                                    f"Replay equity — {ticker} {tf} {sname}"),
                     use_container_width=True)


# ---------------------------------------------------------------------------
# Tab B — Paper Portfolio
# ---------------------------------------------------------------------------

def _candle_fetcher_factory(data_index):
    """Return a function (sym, tf, n) → DataFrame loaded from cached parquets.
    Used for paper trading on cached data — no bridge needed (replay-mode paper)."""
    def fetch(sym: str, tf: str, n: int):
        path = data_index.get(sym, {}).get(tf)
        if path is None:
            raise FileNotFoundError(f"no parquet for {sym} {tf}")
        df = load_parquet(path)
        return df.tail(n).reset_index(drop=True)
    return fetch


def render_paper_tab(strategies, data_index, cfg):
    st.subheader("📡  Paper Portfolio")
    portfolio_default = st.session_state.get(KEY_PROMOTE_TO_PAPER, [])
    if portfolio_default:
        st.info(f"📥 {len(portfolio_default)} entries promoted from Backtest. "
                "Click 'Add' to confirm them or remove individually.")

    with st.form("paper_add"):
        cols = st.columns(4)
        ticker = cols[0].selectbox("ticker", sorted(data_index.keys()),
                                      key="pp_ticker")
        tf = cols[1].selectbox("tf", sorted(data_index[ticker].keys()),
                                  key="pp_tf")
        sname = cols[2].selectbox("strategy", sorted(strategies.keys()),
                                     key="pp_s")
        lots = float(cols[3].number_input("lots",
                                              value=DEFAULT_LOTS.get(ticker, 0.1),
                                              step=0.1, key="pp_lots"))
        if st.form_submit_button("➕  Add to portfolio"):
            entry = {"ticker": ticker, "tf": tf, "strat": sname,
                     "lots": lots, "mpu": DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0),
                     "params": None,
                     "enforce_weekend": cfg.weekend_flat_all,
                     "enforce_daily": True}
            cur = st.session_state.get(KEY_PROMOTE_TO_PAPER, [])
            cur.append(entry)
            st.session_state[KEY_PROMOTE_TO_PAPER] = cur
            st.toast("added")

    portfolio = st.session_state.get(KEY_PROMOTE_TO_PAPER, [])
    if not portfolio:
        st.caption("(no entries yet — promote from Backtest or use the form above)")
        return

    st.markdown("**Current paper portfolio**")
    for i, e in enumerate(portfolio):
        cols = st.columns([2, 1, 1, 1, 1, 1])
        cols[0].markdown(f"**{e['strat']}** • `{e['ticker']}` `{e['tf']}` "
                          f"@ {e['lots']} lots")
        cols[1].caption(f"weekend_flat={'✓' if e['enforce_weekend'] else '✗'}")
        cols[2].caption(f"daily_flat={'✓' if e['enforce_daily'] else '✗'}")
        cols[3].caption(f"mpu=${e['mpu']:.2f}")
        if cols[5].button("🗑", key=f"pp_rm_{i}"):
            portfolio.pop(i)
            st.session_state[KEY_PROMOTE_TO_PAPER] = portfolio
            st.rerun()

    # Start / stop loop
    runs = _paper_runs()
    st.markdown("---")
    cols = st.columns(3)
    if cols[0].button("▶  Start All (paper)", type="primary",
                        use_container_width=True, key="pp_start_all"):
        _start_paper_loop(portfolio, data_index, cfg, runs)
    if cols[1].button("■  Stop All", use_container_width=True, key="pp_stop_all"):
        for run_id, loop in list(runs.items()):
            loop.stop()
        runs.clear()
        st.toast("all paper loops stopped")

    cols[2].markdown(f"**Active loops:** {len(runs)}")
    if runs:
        for run_id, loop in runs.items():
            st.markdown(f"- `{run_id}` — status: **{loop.status}** "
                         f"— {loop.total_open()} open, {loop.total_closed()} closed")
            with st.expander(f"recent events for {run_id}"):
                for ev in reversed(loop.events[-50:]):
                    st.caption(f"`{ev.occurred_at_utc[:19]}` "
                                f"[{ev.kind}] {ev.config_name} {ev.symbol} "
                                f"— {ev.detail}")


def _start_paper_loop(portfolio, data_index, cfg, runs):
    from core.paper_loop import PaperLoop, PaperStrategyConfig
    from dashboards.components.state import discover_strategies as _ds
    strats = _ds()
    configs = []
    for e in portfolio:
        StratCls, ParamsCls = strats[e["strat"]]
        params_obj = e.get("params") or (ParamsCls() if ParamsCls else None)
        strat_inst = StratCls() if params_obj is None else StratCls(params_obj)
        configs.append(PaperStrategyConfig(
            strategy=strat_inst, symbol=e["ticker"], tf=e["tf"],
            lots=e["lots"], money_per_unit_price=e["mpu"],
            name_for_journal=f"{e['strat']}_{e['ticker']}_{e['tf']}",
        ))
    if not configs:
        st.warning("Empty portfolio.")
        return
    tg_cfg = time_guard_cfg_from_risk_config(cfg)
    loop = PaperLoop(
        configs=configs,
        candle_fetcher=_candle_fetcher_factory(data_index),
        db_path=REPO / "data" / "v2.db",
        time_guard_cfg=tg_cfg,
        commission_per_trade=3.0,
        slippage_per_fill_atr_frac=0.1,
        poll_seconds=60.0,
    )
    # For dashboard mode, we tick once synchronously; the user re-runs the
    # page (Streamlit's re-execute button) to advance another tick. Spawning
    # a real thread is fine but Streamlit's session model makes the events
    # log only useful when we tick on this rerun.
    loop.tick()
    runs[loop.run_id] = loop
    st.success(f"started paper loop `{loop.run_id}` — first tick complete. "
                "Re-run the page (or click 'Tick now') to advance another bar.")


# ---------------------------------------------------------------------------
# Tab C — Live (gated)
# ---------------------------------------------------------------------------

def render_live_tab(strategies, data_index, cfg):
    st.subheader("🔴  Live Portfolio")
    db_path = REPO / "data" / "v2.db"

    st.markdown("### Pre-flight checklist")

    # Build the 6 check states
    sentinel = REPO / "data" / "EMERGENCY_STOP"
    checks = []

    # 1. Emergency stop
    es_ok = not sentinel.exists()
    checks.append(("EMERGENCY_STOP file absent", es_ok,
                    f"path: {sentinel}"))

    # 2. Account login allowed (we don't have a live account login yet)
    allowed = cfg.live_safety_allowed_accounts
    checks.append(("Account login in allowlist (or empty list)",
                    True if not allowed else False,
                    f"allowlist: {list(allowed) or '(open)'}"))

    # 3. Daily loss cap
    from core.risk_engine import LiveRiskTracker
    tracker = LiveRiskTracker(db_path=db_path,
                                daily_loss_cap_pct=cfg.daily_loss_cap_pct)
    daily = tracker.account_daily_loss_pct()
    checks.append((f"Daily loss < {cfg.daily_loss_cap_pct}% cap",
                    daily < cfg.daily_loss_cap_pct,
                    f"current: {daily:.2f}%"))

    # 4. Reconciliation green on last backtest (session-state)
    last_bt = st.session_state.get("_last_backtest")
    if last_bt is None:
        checks.append(("Last backtest reconciles", False,
                        "no backtest run in this session"))
    else:
        r = last_bt["result"]
        checks.append(("Last backtest reconciles",
                        r.reconciles,
                        f"divergence ${abs(r.sum_realized_pnl - r.equity_curve_pnl):.4f}"))

    # 5. Replay parity recent
    pg = ParityGate(db_path)
    sname_for_check = st.text_input("strategy to live-trade",
                                       value="vol_breakout",
                                       key="live_sname")
    last_pass = pg.last_pass_for(sname_for_check)
    parity_ok = pg.is_recent(sname_for_check, max_age_hours=24.0)
    if last_pass is None:
        parity_detail = "no parity pass on record"
    else:
        ts, div = last_pass
        parity_detail = f"last pass {ts.strftime('%Y-%m-%d %H:%M UTC')}, div=${div:.4f}"
    override = st.checkbox("Override parity recency (Phase 5 ONLY)",
                              value=st.session_state.get(KEY_OVERRIDE_PARITY_RECENCY, False),
                              key="live_override_parity")
    st.session_state[KEY_OVERRIDE_PARITY_RECENCY] = override
    parity_check_ok = parity_ok or override
    checks.append((f"Replay-parity for `{sname_for_check}` < 24h",
                    parity_check_ok,
                    parity_detail + (" (overridden)" if override and not parity_ok else "")))

    # 6. Not in no-entry window
    from core.time_guards import in_no_entry_window
    tg_cfg = time_guard_cfg_from_risk_config(cfg)
    in_win = in_no_entry_window(datetime.now(timezone.utc), tg_cfg)
    checks.append(("Not in no-entry window before US close", not in_win,
                    "currently in window" if in_win else "outside window"))

    # FTMO-test acknowledge checkbox
    ack = st.checkbox(
        "I understand this account already failed once and may fail again.",
        value=st.session_state.get(KEY_FTMO_RISK_ACCEPTED, False),
        key="live_ack",
    )
    st.session_state[KEY_FTMO_RISK_ACCEPTED] = ack

    for label, ok, detail in checks:
        emoji = "✅" if ok else "⛔"
        st.markdown(f"{emoji}  **{label}** — {detail}")

    all_ok = all(c[1] for c in checks) and ack
    st.markdown("---")
    if all_ok:
        st.success("All required checks green AND FTMO risk accepted. "
                    "Live trading can be started.")
        st.button("🔴  Start Live (NOT WIRED IN THIS BUILD)",
                    disabled=True,
                    help="The live executor is fully tested but only mocked — "
                         "real bridge wiring is the user's deployment step.")
    else:
        st.error("One or more pre-flight checks are RED. "
                  "Live trading is BLOCKED.")

    # Sticky emergency stop button
    st.markdown("---")
    cols = st.columns([3, 1])
    cols[0].markdown("**Emergency stop sentinel** — touch this file from any "
                      "process (or this button) to refuse all new live orders. "
                      f"Path: `{sentinel}`")
    if cols[1].button("⛔  Touch EMERGENCY_STOP",
                        type="primary", key="live_estop"):
        sentinel.touch()
        st.toast("EMERGENCY_STOP file created")
        st.rerun()


# ---------------------------------------------------------------------------
# Tab D — MT5 Tester Parity
# ---------------------------------------------------------------------------

def render_mt5_parity_tab():
    st.subheader("🧪  MT5 Strategy Tester Parity")
    st.markdown("""
This tab tracks whether each strategy has a MATCHING MT5 Strategy Tester
report on file. The pipeline (Phase 26):

1. `python scripts/strategy_to_mql5.py --strategy <name> --output mql5/<Name>.mq5`
2. Compile in MetaEditor (F7), drop on a chart matching the strategy's
   ticker/TF.
3. Run MT5 Strategy Tester with "Every tick based on real ticks" — same
   date range as your Python backtest.
4. Right-click the report → Save → CSV.
5. Save to `data/mt5_tester_reports/<strategy>_<ticker>_<tf>.csv`.
6. `python scripts/mt5_parity_check.py --strategy <name> --ticker <s> --tf <tf>`

Until this pipeline is run for a strategy, the row stays ⚠️.
""")
    reports_dir = REPO / "data" / "mt5_tester_reports"
    if not reports_dir.exists():
        st.info(f"`{reports_dir.relative_to(REPO)}` does not exist yet "
                 "(no parity reports recorded).")
        return
    rows = []
    for path in sorted(reports_dir.glob("*.md")):
        rows.append({"report": path.name,
                     "modified": datetime.fromtimestamp(
                         path.stat().st_mtime, tz=timezone.utc
                     ).isoformat(timespec='seconds')})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.caption("(no parity reports found)")


def main():
    st.set_page_config(page_title="Paper / Live", page_icon="📡", layout="wide")
    st.title("📡  Paper & Live")
    cfg = load_config()
    strats = discover_strategies()
    data_index = discover_data()
    tab_a, tab_b, tab_c, tab_d = st.tabs([
        "🎬  Replay", "📡  Paper portfolio", "🔴  Live", "🧪  MT5 parity",
    ])
    with tab_a:
        render_replay_tab(strats, data_index, cfg)
    with tab_b:
        render_paper_tab(strats, data_index, cfg)
    with tab_c:
        render_live_tab(strats, data_index, cfg)
    with tab_d:
        render_mt5_parity_tab()


main()
