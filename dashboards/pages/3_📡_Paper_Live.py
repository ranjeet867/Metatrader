"""
3_📡_Paper_Live.py — Replay (Tab A) | Paper Portfolio (Tab B) | Live (Tab C)
                  + MT5 Tester Parity (Tab D)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import run_backtest   # noqa: E402
from core.config import load_config   # noqa: E402
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
    """Simplified Live deployment view.

    Old design: a wall of pre-flight checks, a text-input for strategy name,
    one disabled Start button. Confusing and didn't show the operator
    *what* to deploy or *what stats* each strategy had.

    New design:
      1. Compact one-line health pill — "All checks ✅ (expand to inspect)".
      2. The strategy LIBRARY as a checklist — recommended ones pre-checked,
         each row showing PF / R / n_test / why, ticker + tf locked from the
         library row, risk % editable, computed lots displayed live.
      3. A single big "Deploy selected to Live" button.

    This is the operations centre — Operations page (Page 0) is the daily
    monitoring view, this is the "I want to add / change which strategies
    are running" view.
    """
    from core import account_manager
    from core import strategy_library

    st.subheader("🔴  Deploy live")
    st.caption(
        "Pick which strategies to run. Each row's stats come from the "
        "latest grid sweep on real broker data — `make sweep-grid` to "
        "refresh. Lots are auto-computed from your account equity × risk %.")
    db_path = REPO / "data" / "v2.db"

    # ------- 1. Compact health pill -------
    sentinel = REPO / "data" / "EMERGENCY_STOP"
    from core.risk_engine import LiveRiskTracker
    from core.time_guards import in_no_entry_window
    tracker = LiveRiskTracker(db_path=db_path,
                               daily_loss_cap_pct=cfg.daily_loss_cap_pct)
    daily = tracker.account_daily_loss_pct()
    tg_cfg = time_guard_cfg_from_risk_config(cfg)
    in_win = in_no_entry_window(datetime.now(timezone.utc), tg_cfg)

    checks = [
        ("EMERGENCY_STOP absent", not sentinel.exists()),
        (f"Daily loss < {cfg.daily_loss_cap_pct}% cap "
         f"(now {daily:.2f}%)", daily < cfg.daily_loss_cap_pct),
        ("Outside no-entry window", not in_win),
    ]
    n_ok = sum(1 for _, ok in checks if ok)
    n_total = len(checks)
    pill_color = ("#15803d" if n_ok == n_total
                   else "#b08800" if n_ok >= n_total - 1 else "#7f1d1d")
    pill_text = (f"All checks ✅ ({n_ok}/{n_total})" if n_ok == n_total
                  else f"⚠ {n_total - n_ok} check(s) failing ({n_ok}/{n_total})")
    st.markdown(
        f"<div style='display:inline-block;background:{pill_color};color:white;"
        f"padding:6px 14px;border-radius:14px;font-weight:600;font-size:0.86rem;"
        f"font-family:ui-monospace,Menlo,monospace;letter-spacing:0.04em;'>"
        f"{pill_text}</div>",
        unsafe_allow_html=True,
    )
    with st.expander("Inspect pre-flight checks"):
        for label, ok in checks:
            st.markdown(("✅" if ok else "⛔") + f" {label}")
        if sentinel.exists():
            st.warning(
                "EMERGENCY_STOP is active — clear it on the Operations "
                "page to allow live orders.")

    # ------- 2. Strategy library multiselect -------
    st.markdown("### Strategy portfolio")
    lib = strategy_library.list_library()
    if not lib:
        st.warning(
            "No strategies in library — run `make sweep-grid` to populate "
            "`docs/grid_results.md`.")
        return
    df = strategy_library.to_dataframe(lib)

    # Defaults: every recommended row pre-checked, others unchecked.
    default_selected = [e.slug for e in lib if e.recommended]
    state_key = "_live_selected_slugs"
    if state_key not in st.session_state:
        st.session_state[state_key] = list(default_selected)

    # Compact picker
    pick_cols = st.columns([3, 2, 2])
    if pick_cols[0].button("✅  Select all recommended",
                            use_container_width=True):
        st.session_state[state_key] = list(default_selected)
        st.rerun()
    if pick_cols[1].button("Clear all", use_container_width=True):
        st.session_state[state_key] = []
        st.rerun()
    risk_default = float(pick_cols[2].number_input(
        "Default risk per trade %",
        value=0.30, step=0.05, format="%.2f",
        min_value=0.05, max_value=2.0,
        help="Applied to every strategy on first selection. Override per "
              "row below.",
    ))

    # Determine the active account for sizing math
    try:
        accounts = account_manager.list_accounts()
        active = accounts[0] if accounts else None
    except Exception:
        active = None
    if active is None:
        st.info(
            "Add an MT5 account on the Operations page to size positions. "
            "Showing portfolio with placeholder $100,000 equity.")
        equity = 100_000.0
    else:
        equity = float(active.effective_baseline_equity)
        st.caption(
            f"Sizing against **{active.alias}** "
            f"(baseline ${equity:,.0f}). Change baseline in Operations → "
            f"Settings.")

    # ------- 3. Per-row card with checkbox + risk + computed lots -------
    selected_slugs: list[str] = []
    for entry in lib:
        es = entry.edge
        with st.container(border=True):
            row = st.columns([0.5, 4, 1.5, 1.5, 1.5, 1.5])
            checked = row[0].checkbox(
                "", value=(entry.slug in st.session_state[state_key]),
                key=f"chk_{entry.slug}", label_visibility="collapsed",
            )
            star = "⭐ " if entry.recommended else ""
            edge_chip = ("<span style='background:#15803d;color:white;"
                         "padding:2px 6px;border-radius:4px;"
                         "font-size:0.7rem;font-weight:600;'>EDGE</span>"
                         if es and es.is_survivor else "")
            row[1].markdown(
                f"{star}**`{entry.strategy}`**  "
                f"on `{entry.ticker}` · `{entry.tf}` · "
                f"{'long-only' if entry.long_only else 'bidir'}  "
                f"{edge_chip}",
                unsafe_allow_html=True,
            )
            if es is not None:
                row[2].markdown(
                    f"<div style='font-family:ui-monospace,Menlo,monospace;"
                    f"font-size:0.78rem;font-variant-numeric:tabular-nums;"
                    f"line-height:1.4;'>"
                    f"<span style='color:#9ca3af;'>PF</span> "
                    f"{es.test_pf:.2f}<br>"
                    f"<span style='color:#9ca3af;'>R</span> "
                    f"<b>{es.test_r:+.2f}</b></div>",
                    unsafe_allow_html=True,
                )
                row[3].markdown(
                    f"<div style='font-family:ui-monospace,Menlo,monospace;"
                    f"font-size:0.78rem;font-variant-numeric:tabular-nums;"
                    f"line-height:1.4;'>"
                    f"<span style='color:#9ca3af;'>n_test</span> "
                    f"{es.n_test}<br>"
                    f"<span style='color:#9ca3af;'>train R</span> "
                    f"{es.train_r:+.2f}</div>",
                    unsafe_allow_html=True,
                )
            else:
                row[2].caption("(no stats)")
                row[3].caption("")
            risk_pct = float(row[4].number_input(
                "risk %", value=risk_default, step=0.05, format="%.2f",
                min_value=0.05, max_value=2.0,
                key=f"risk_{entry.slug}", label_visibility="collapsed",
            ))

            # Compute approximate lots — uses a coarse stop estimate of
            # 1×ATR ≈ 1% of price. Real lots come from the strategy at
            # signal-time; this is a sanity-check display only.
            risk_dollars = equity * risk_pct / 100.0
            row[5].markdown(
                f"<div style='font-family:ui-monospace,Menlo,monospace;"
                f"font-size:0.78rem;font-variant-numeric:tabular-nums;'>"
                f"<span style='color:#9ca3af;'>risk $</span> "
                f"<b>{risk_dollars:,.0f}</b><br>"
                f"<span style='color:#9ca3af;'>lots</span> "
                f"<i>signal-time</i></div>",
                unsafe_allow_html=True,
            )
            if entry.why:
                st.caption(entry.why)
            if checked:
                selected_slugs.append(entry.slug)

    # Persist selection
    st.session_state[state_key] = selected_slugs

    # ------- 4. Deploy button -------
    st.markdown("---")
    cols = st.columns([3, 1])
    cols[0].markdown(
        f"**{len(selected_slugs)}** strategies selected.  Total daily-cap "
        f"budget at default risk: **{len(selected_slugs) * risk_default:.2f}%** "
        f"({len(selected_slugs) * risk_default * 0.01 * equity:,.0f} $).")
    can_deploy = (len(selected_slugs) > 0
                  and all(ok for _, ok in checks))
    if cols[1].button("🚀 Deploy to live", type="primary",
                       disabled=not can_deploy,
                       use_container_width=True,
                       help="Pre-flight gates must be green. Use the "
                            "Operations page to clear EMERGENCY_STOP."):
        # Wire each selection into deployments.json for the active account
        if active is None:
            st.error("Add an MT5 account first.")
        else:
            from core import deployment as dep_mod
            for slug in selected_slugs:
                entry = next((e for e in lib if e.slug == slug), None)
                if entry is None:
                    continue
                dep_id = (dep_mod.Deployment.slug(
                    entry.strategy, entry.ticker, entry.tf)
                    + ("_long" if entry.long_only else "_bidir"))
                risk = float(st.session_state.get(
                    f"risk_{slug}", risk_default))
                d = dep_mod.Deployment(
                    deployment_id=dep_id,
                    strategy=entry.strategy,
                    ticker=entry.ticker, tf=entry.tf,
                    long_only=entry.long_only,
                    params={"long_only": entry.long_only},
                    risk_pct=risk, daily_cap_pct=cfg.daily_loss_cap_pct,
                    status="live",   # NB: actual order routing still gated
                )
                dep_mod.upsert_deployment(active.login, d)
            st.success(
                f"Deployed {len(selected_slugs)} strategies to "
                f"#{active.login}. View on the Operations page.")
            st.toast("🚀 Live portfolio updated.")


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
