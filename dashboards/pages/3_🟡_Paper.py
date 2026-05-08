"""
3_🟡_Paper.py — dedicated PAPER trading page.

Purpose: a single screen where the operator can (a) see every paper
deployment with full controls, (b) see how much each strategy made in
paper, (c) drill into any strategy's equity curve. The Live page (Page 9)
is a separate top-level page so paper actions can never be confused for
live actions.

Sections:
  1. Headline KPIs — total paper P&L, n_strategies, win rate
  2. Per-deployment cards — risk, daily cap, status pill, ▶/■/Promote/Halt
  3. Per-strategy P&L bar chart
  4. Strategy equity curve (drop-down to pick which one to view)
  5. Replay-parity helper — explains why parity is required + run it inline
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import account_manager                                # noqa: E402
from core import deployment as dep_mod                          # noqa: E402
from core import perf_query                                     # noqa: E402
from dashboards.components import theme                         # noqa: E402

# Bug D fix (2026-05-08): the runner writes trades / bridge_events /
# forensic_snapshots to the per-account DB at data/accounts/{login}/v2.db,
# NOT to the legacy main repo DB. Hardcoding DB_PATH = REPO/"data"/"v2.db"
# caused this page to query an empty DB while the runner wrote elsewhere
# — closed trades + equity curve + per-strategy P&L all came up as 0.
# Resolve the active-account DB path at import time. Falls back to the
# main repo DB only if no account is configured (test / CI).
def _resolve_active_db_path():
    try:
        accounts = account_manager.list_accounts()
        if accounts:
            return account_manager.get_db_path(accounts[0].login)
    except Exception:
        pass
    return REPO / "data" / "v2.db"


DB_PATH = _resolve_active_db_path()
MODE = "paper"


# NOTE: status pill rendering moved to
# `dashboards.components.deployment_card.status_pill` so Paper and
# Live use the same colours. The local helper that used to live here
# was identical to the one in 9_🟢_Live.py — dropped to prevent drift.


def _render_headline(account, paper_summary: dict) -> None:
    cols = st.columns(5)
    cols[0].metric("Account", f"#{account.login}",
                      help=account.alias)
    cols[1].metric("Paper P&L (all-time)",
                      f"${paper_summary['total_pnl']:+,.2f}")
    cols[2].metric("Closed trades", paper_summary["n_trades"])
    cols[3].metric("Win rate", f"{paper_summary['win_rate_pct']:.1f}%")
    cols[4].metric("Strategies running",
                      f"{paper_summary['n_strategies']}",
                      help=f"{paper_summary['n_winners']} profitable / "
                            f"{paper_summary['n_losers']} losing")


def _render_deployments(account, deployments) -> None:
    """Per-deployment cards with controls."""
    if not deployments:
        st.info(
            "No deployments configured for this account. Configure on the "
            "**Operations** page or pick recommendations from the "
            "**Strategy Library**.")
        return

    show_all = st.toggle("Show ALL statuses (not just paper/idle)",
                          value=False, key="paper_show_all")
    if show_all:
        rows_in_view = list(deployments)
    else:
        rows_in_view = [d for d in deployments
                         if d.status in ("paper", "idle", "paused")]

    n_paper = sum(1 for d in deployments if d.status == "paper")
    summary = st.columns(4)
    summary[0].metric("All deployments", len(deployments))
    summary[1].metric("🟡 Paper running", n_paper)
    summary[2].metric("⚪ Idle", sum(1 for d in deployments if d.status == "idle"))
    summary[3].metric("⏸ Paused", sum(1 for d in deployments if d.status == "paused"))

    st.markdown(f"### Paper deployments ({len(rows_in_view)} shown)")
    for d in rows_in_view:
        _render_one_deployment_card(account, d)

    # Bulk actions
    st.markdown("---")
    bulk = st.columns(4)
    if bulk[0].button("▶  Start ALL idle as paper", type="primary",
                       width="stretch", key="paper_bulk_start"):
        for d in deployments:
            if d.status in ("idle", "paused"):
                dep_mod.update_status(account.login, d.deployment_id, "paper")
        st.toast("started paper for all idle deployments")
        st.rerun()
    if bulk[1].button("■  Stop ALL paper",
                       width="stretch", key="paper_bulk_stop"):
        for d in deployments:
            if d.status == "paper":
                dep_mod.update_status(account.login, d.deployment_id, "idle")
        st.toast("stopped all paper")
        st.rerun()
    cap_total = sum(d.daily_cap_pct
                     for d in deployments if d.status == "paper")
    risk_total = sum(d.risk_pct
                      for d in deployments if d.status == "paper")
    bulk[2].metric("Aggregate daily cap", f"{cap_total:.1f}%",
                     help="Sum of daily caps across active paper deployments. "
                           "Account FTMO cap is 5%.")
    bulk[3].metric("Aggregate per-trade risk", f"{risk_total:.2f}%",
                     help="If all paper deployments fired the same bar, "
                           "this is the equity at risk in % of baseline.")


def _render_one_deployment_card(account, d) -> None:
    """Render one paper deployment card via the shared
    `deployment_card.render_card` component (single source of truth
    used by both Paper and Live pages, so they always look identical
    except for the action button row)."""
    from core.parity_gate import ParityGate
    from dashboards.components import deployment_card as dc_mod
    gate = ParityGate(REPO / "data" / "v2.db")
    dc_mod.render_card(
        account, d,
        mode="paper",
        parity_passed=gate.is_recent(d.strategy),
    )


def _per_strategy_bar(rows: list[perf_query.StrategyPnL]) -> go.Figure:
    if not rows:
        return go.Figure().update_layout(height=240,
                                            title="(no closed trades yet)")
    rows_sorted = sorted(rows, key=lambda r: r.total_pnl)
    labels = [f"{r.strategy}<br>{r.ticker} {r.tf}" for r in rows_sorted]
    values = [r.total_pnl for r in rows_sorted]
    colors = ["#16a34a" if v >= 0 else "#dc2626" for v in values]
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h",
        marker_color=colors,
        text=[f"${v:+,.0f}" for v in values],
        textposition="outside",
        hovertemplate="%{y}<br>P&L: $%{x:+,.2f}<extra></extra>",
    ))
    fig.update_layout(
        title="Paper P&L by strategy",
        height=max(220, 30 * len(rows_sorted) + 60),
        margin=dict(l=10, r=10, t=40, b=10),
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937", title="$ realized"),
        yaxis=dict(gridcolor="#1f2937"),
    )
    return fig


def _equity_chart(eq: pd.DataFrame, *, label: str,
                    starting_balance: float) -> go.Figure:
    if eq.empty:
        return go.Figure().update_layout(height=240,
                                            title="(no closed trades)")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["time_utc"], y=eq["equity"], mode="lines",
        name="equity", line=dict(color="#38bdf8", width=2),
        fill="tozeroy", fillcolor="rgba(56,189,248,0.10)",
    ))
    fig.add_hline(y=starting_balance,
                   line=dict(color="#6b7280", dash="dash"),
                   annotation_text=f"start ${starting_balance:,.0f}",
                   annotation_position="top right",
                   annotation_font=dict(color="#6b7280", size=10))
    fig.update_layout(
        title=f"Equity curve — {label}",
        height=320, margin=dict(l=10, r=10, t=40, b=10),
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937"),
        yaxis=dict(gridcolor="#1f2937", title="$ equity"),
    )
    return fig


def _render_per_strategy_breakdown(account) -> None:
    rows = perf_query.per_strategy_pnl(DB_PATH, mode=MODE)
    st.plotly_chart(_per_strategy_bar(rows), width="stretch")

    if rows:
        # Detailed table
        df = pd.DataFrame([{
            "Strategy": r.strategy,
            "Ticker": r.ticker,
            "TF": r.tf,
            "Trades": r.n_trades,
            "Win%": round(r.win_rate_pct, 1),
            "Total P&L $": round(r.total_pnl, 2),
            "Avg win $": round(r.avg_win, 2),
            "Avg loss $": round(r.avg_loss, 2),
            "Largest win $": round(r.largest_win, 2),
            "Largest loss $": round(r.largest_loss, 2),
            "Last trade UTC": r.last_trade_utc or "—",
        } for r in rows])
        st.dataframe(df, width="stretch",
                       height=min(500, 38 * (len(df) + 1)))

    # Per-strategy equity-curve drop-down
    st.markdown("### 📈  Strategy equity curve")
    if not rows:
        st.caption("(no closed trades — nothing to chart yet)")
        return
    pick_options = ["ALL paper combined"] + [f"{r.strategy} · {r.ticker} · {r.tf}"
                                                for r in rows]
    pick = st.selectbox("Pick a strategy to view its equity curve",
                          pick_options, key="paper_eq_pick")
    starting = float(account.effective_baseline_equity)
    if pick == "ALL paper combined":
        eq = perf_query.equity_curve(DB_PATH, mode=MODE,
                                       starting_balance=starting)
        label = "All paper strategies combined"
    else:
        idx = pick_options.index(pick) - 1
        r = rows[idx]
        eq = perf_query.equity_curve(DB_PATH, mode=MODE,
                                       strategy=r.strategy, ticker=r.ticker,
                                       starting_balance=starting)
        label = pick
    st.plotly_chart(_equity_chart(eq, label=label,
                                     starting_balance=starting),
                     width="stretch")


def _render_replay_parity_helper(strategies, data_index, cfg) -> None:
    """Inline explanation + run-now button for replay-parity."""
    with st.expander(
        "❓  What is replay-parity? (and why your strategy may be blocked)",
        expanded=False,
    ):
        st.markdown(
            "**Replay-parity** is a v2 safety invariant: before any "
            "strategy can go live, its backtest run must match the same "
            "data played back bar-by-bar through the live engine — "
            "within **$0.01** divergence.\n\n"
            "Why it matters:\n"
            "- Catches silent bugs where the live execution path drifts "
            "from the backtest engine.\n"
            "- Means the equity curve you saw in backtest is the equity "
            "curve you'll get in paper / live.\n\n"
            "How to record a parity pass for a strategy:\n"
            "1. Pick the strategy + ticker + tf below.\n"
            "2. Hit **Run replay-parity check** — it runs both backtest "
            "and bar-by-bar replay.\n"
            "3. If divergence < $0.01, the pass is logged automatically. "
            "The Live page will then accept it.\n"
        )
    cols = st.columns([1, 1, 1, 1])
    if not data_index:
        st.caption("(no parquets to replay against)")
        return
    ticker = cols[0].selectbox("ticker", sorted(data_index.keys()),
                                  key="parity_ticker")
    tf = cols[1].selectbox("tf", sorted(data_index[ticker].keys()),
                              key="parity_tf")
    sname = cols[2].selectbox("strategy", sorted(strategies.keys()),
                                 key="parity_strat")
    if cols[3].button("Run replay-parity check", type="primary",
                        width="stretch", key="parity_run"):
        _do_parity_run(strategies, data_index, sname, ticker, tf, cfg)


def _do_parity_run(strategies, data_index, sname, ticker, tf, cfg):
    from core.data import load_parquet
    from core.parity_check import run_parity
    from core.parity_gate import ParityGate
    from core.symbol_info_loader import try_load as try_load_symbol_info
    from dashboards.components.state import (
        DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit,
    )

    StratCls, ParamsCls = strategies[sname]
    df = load_parquet(data_index[ticker][tf])
    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    sym_info = try_load_symbol_info(ticker)
    pr = run_parity(
        df, strat, symbol=ticker, tf=tf,
        starting_balance=100_000,
        lots=DEFAULT_LOTS.get(ticker, 0.1),
        money_per_unit_price=resolve_money_per_unit(ticker),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        risk_cfg=cfg, symbol_info=sym_info,
    )
    div = pr.divergence_dollars
    if div < 0.01:
        ParityGate(DB_PATH).record_pass(sname, divergence_dollars=div)
        st.success(f"✅  Parity PASSED — divergence ${div:.4f}. "
                    f"`{sname}` is now cleared for live deployment.")
    else:
        st.error(f"⛔  Parity FAILED — divergence ${div:.2f}. "
                  f"`{sname}` is blocked from live. Investigate the "
                  f"engine drift before retrying.")


def _render_auto_demoted_banner(account, deployments) -> None:
    """Show a banner listing deployments that were AUTO-DEMOTED from
    LIVE → PAPER by the runner because their replay-parity expired.
    Each row gets a 🔬 Run parity + 🟢 Promote-back-to-live button so
    the user can fix the issue without leaving this page.
    """
    demoted = []
    for d in deployments:
        if d.status != "paper":
            continue
        notes = (d.notes or "")
        if "[AUTO-DEMOTED LIVE→PAPER" in notes:
            demoted.append(d)
    if not demoted:
        return
    with st.container(border=True):
        st.warning(
            f"⚠ **{len(demoted)} deployment(s) were auto-demoted from "
            f"LIVE → PAPER** because their replay-parity expired (>24h "
            f"since last pass). They're now running in PAPER mode. To "
            f"put them back on LIVE: run replay-parity (refreshes the "
            f"gate), then click 🟢 Promote → live."
        )
        from core.parity_gate import ParityGate
        from dashboards.components.state import discover_strategies
        from dashboards.components.strategy_resolver import (
            resolve_base_strategy,
        )
        gate = ParityGate(DB_PATH)
        strats = discover_strategies()
        for d in demoted:
            base = (resolve_base_strategy(d.strategy, strats)
                     or d.strategy)
            parity_ok = gate.is_recent(base)
            row = st.columns([3, 2, 2, 2])
            row[0].markdown(
                f"**`{d.strategy}`** · `{d.ticker}` `{d.tf}` "
                f"({'long' if d.long_only else 'bidir'})"
            )
            if parity_ok:
                last = gate.last_pass_for(base)
                ts = last[0].strftime("%H:%M") if last else "?"
                row[1].markdown(
                    f"<span style='color:#16a34a;'>✅ parity OK "
                    f"(last {ts})</span>",
                    unsafe_allow_html=True,
                )
            else:
                row[1].markdown(
                    f"<span style='color:#dc2626;'>⛔ parity stale</span>",
                    unsafe_allow_html=True,
                )
            # 🔬 Run parity now — opens the same dialog Live page uses
            if row[2].button(
                "🔬 Run parity",
                key=f"_paper_demote_parity_{d.deployment_id}",
                width="stretch",
            ):
                st.session_state["_paper_parity_target"] = {
                    "variant": d.strategy,
                    "base": base,
                    "ticker": d.ticker,
                    "tf": d.tf,
                    "long_only": d.long_only,
                }
                st.rerun()
            # 🟢 Promote → live: parity recommended but no longer
            # blocking. If parity is stale, show acknowledge checkbox
            # so user can override consciously.
            ack_key = f"_paper_promote_ack_{d.deployment_id}"
            can_promote = parity_ok or st.session_state.get(ack_key, False)
            if not parity_ok:
                row[3].checkbox(
                    "⚠ ack: no parity",
                    key=ack_key,
                    help=(f"Acknowledge `{d.strategy}` has no recent "
                          f"parity pass. Recommended to run parity "
                          f"first (button above) but not required."),
                )
            promote_label = ("🟢 Promote → live" if parity_ok
                                else "🟢 Promote anyway ⚠")
            if row[3].button(
                promote_label,
                key=f"_paper_promote_{d.deployment_id}",
                width="stretch",
                type="primary" if parity_ok else "secondary",
                disabled=not can_promote,
                help=("Run parity above OR tick the ack checkbox to "
                      "override" if not parity_ok else None),
            ):
                # Strip the AUTO-DEMOTED note so it doesn't show again
                cleaned_notes = "\n".join(
                    line for line in (d.notes or "").splitlines()
                    if "[AUTO-DEMOTED LIVE→PAPER" not in line
                ).strip()
                d.notes = cleaned_notes
                d.status = "live"
                dep_mod.upsert_deployment(account.login, d)
                st.toast(
                    f"✅ {d.deployment_id} promoted back to LIVE",
                )
                st.rerun()

    # Render the parity dialog if a target is queued
    target = st.session_state.get("_paper_parity_target")
    if target is not None:
        _render_paper_parity_dialog(target)


@st.dialog("Replay-parity", width="large")
def _render_paper_parity_dialog(target: dict) -> None:
    """Run replay-parity for a queued deployment. Same modal pattern
    as the Live page version — PASS records to the gate, FAIL stays
    open with the diagnosis."""
    import time
    from core.config import load_config
    from core.data import load_parquet
    from core.parity_check import run_parity
    from core.parity_gate import ParityGate
    from core.symbol_info_loader import try_load as try_load_symbol_info
    from dashboards.components.state import (
        DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit, discover_strategies,
    )

    base = target["base"]
    ticker = target["ticker"]
    tf = target["tf"]
    st.markdown(
        f"### `{target['variant']}`  ×  `{ticker}`  `{tf}`"
    )
    st.caption(f"Base strategy: **`{base}`**")
    strats = discover_strategies()
    if base not in strats:
        st.error(f"⛔ Base strategy `{base}` not registered.")
        if st.button("Close", width="stretch"):
            st.session_state.pop("_paper_parity_target", None)
            st.rerun()
        return
    parquet = REPO / "data" / f"{ticker}_{tf}.parquet"
    if not parquet.exists():
        st.error(f"⛔ No parquet for {ticker} {tf}.")
        if st.button("Close", width="stretch"):
            st.session_state.pop("_paper_parity_target", None)
            st.rerun()
        return
    StratCls, ParamsCls = strats[base]
    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    cfg = load_config()
    df = load_parquet(parquet)
    sym_info = try_load_symbol_info(ticker)
    t0 = time.time()
    with st.spinner(f"Running parity for {base}..."):
        pr = run_parity(
            df, strat, symbol=ticker, tf=tf,
            starting_balance=100_000,
            lots=DEFAULT_LOTS.get(ticker, 0.1),
            money_per_unit_price=resolve_money_per_unit(ticker),
            commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
            risk_cfg=cfg, symbol_info=sym_info,
        )
    elapsed = time.time() - t0
    cols = st.columns(4)
    cols[0].metric("BT trades", pr.bt.n_trades)
    cols[1].metric("Replay trades", pr.rp.n_trades)
    cols[2].metric("BT P&L", f"${pr.bt.sum_realized_pnl:+,.2f}")
    cols[3].metric("Divergence", f"${pr.divergence_dollars:.4f}")
    if pr.passes:
        ParityGate(DB_PATH).record_pass(
            base, divergence_dollars=pr.divergence_dollars,
        )
        st.success(
            f"✅ Parity PASSED in {elapsed:.2f}s — `{base}` cleared "
            f"for live deployment for the next 24h. Click 🟢 Promote "
            f"→ live above to put this deployment back on live."
        )
    else:
        st.error(
            f"⛔ Parity FAILED — divergence ${pr.divergence_dollars:.4f} "
            f"> $0.01. `{base}` BLOCKED from live."
        )
    if st.button("Close", width="stretch"):
        st.session_state.pop("_paper_parity_target", None)
        st.rerun()


def main() -> None:
    st.set_page_config(page_title="Paper", page_icon="🟡", layout="wide")
    theme.inject_css()
    st.title("🟡  Paper Trading")
    st.caption(
        "Per-strategy paper-trade controls + analytics. Live trading is "
        "on a separate page so the actions can't be confused.")
    st.info(
        "ℹ️ **Risk %, daily cap %, and max-lots are also editable on "
        "the [⚙️ Settings](/Settings) page** (Section D — Per-deployment "
        "caps). Editing here works too; both write to the same file. "
        "Future cleanup will consolidate edits to Settings.",
        icon="ℹ️",
    )

    # Active account
    try:
        accounts = account_manager.list_accounts()
        active = accounts[0] if accounts else None
    except Exception:
        active = None
    if active is None:
        st.warning("No MT5 account configured — add one on the "
                    "Operations page → Settings tab.")
        return

    # ---- 1. Headline ----
    summary = perf_query.overall_summary(DB_PATH, mode=MODE)
    _render_headline(active, summary)

    # ---- 2. Per-deployment cards ----
    st.markdown("---")
    try:
        deployments = dep_mod.load_deployments(active.login)
    except Exception as e:
        st.error(f"Could not load deployments: {e}")
        deployments = []

    # ── Auto-demotion banner ────────────────────────────────────────
    # Surface deployments that were auto-demoted from LIVE → PAPER by
    # the runner because their replay-parity expired. Without this, the
    # demotion is silent and the user has no idea why their live
    # deployments disappeared from the Live page.
    _render_auto_demoted_banner(active, deployments)

    _render_deployments(active, deployments)

    # ---- 3. Per-strategy P&L breakdown ----
    st.markdown("---")
    st.markdown("## 📊  Per-strategy paper P&L")
    _render_per_strategy_breakdown(active)

    # ---- 4. Replay-parity helper ----
    st.markdown("---")
    st.markdown("## 🎬  Replay-parity (required before going live)")
    from core.config import load_config
    from dashboards.components.state import discover_data, discover_strategies
    cfg = load_config()
    strategies = discover_strategies()
    data_index = discover_data()
    _render_replay_parity_helper(strategies, data_index, cfg)


main()
