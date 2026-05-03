"""
0_🚀_Operations.py — the trader's front door.

Multi-account view: switch FTMO accounts, see deployment cards (one per
strategy×ticker×tf), manage open positions, see today's broker statement,
and go live with typed-confirm pre-flight gates.

Numbered 0 so Streamlit auto-discovery puts it first.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import account_manager, deployment as dep_mod   # noqa: E402
from core.account_statement import StatementReader   # noqa: E402
from core.config import load_config   # noqa: E402
from core.deployment import Deployment   # noqa: E402
from core.ftmo_clock import FtmoClock, FtmoRules   # noqa: E402
from core.journal import JournalWriter   # noqa: E402
from core.mt5_account import MT5AccountClient   # noqa: E402
from core.parity_gate import ParityGate   # noqa: E402
from core.position_manager import PositionManager   # noqa: E402
from core.risk_engine import LiveRiskTracker   # noqa: E402
from core.time_guards import time_guard_cfg_from_risk_config   # noqa: E402
from dashboards.components import (   # noqa: E402
    account_switcher,
    deployment_card,
    emergency_stop_bar,
    ftmo_progress,
    go_live_modal,
    live_equity_chart,
    position_manager_panel,
    statement_panel,
)
from dashboards.components.account_switcher import (   # noqa: E402
    get_active_login,
)
from dashboards.components.state import (   # noqa: E402
    discover_strategies,
)


@st.cache_resource
def _bridge() -> MT5AccountClient:
    return MT5AccountClient()


def _build_clock(account: account_manager.Account) -> FtmoClock:
    try:
        tz = ZoneInfo(account.user_tz)
    except Exception:
        tz = ZoneInfo("UTC")
    return FtmoClock(FtmoRules(user_tz=tz))


def _ensure_seed_deployments(login: int) -> None:
    """First-time visitors get the 6 survivors pre-filled."""
    if not dep_mod.load_deployments(login):
        dep_mod.seed_survivor_deployments(login)


def _account_card(account: account_manager.Account, summary, container) -> None:
    container.markdown(f"### 🏦 {account.alias}")
    container.caption(f"login `{account.login}` · type `{account.type}`"
                        f" · tz `{account.user_tz}`")
    if summary is None:
        container.info("Bridge offline — account info unavailable.")
        return
    cols = container.columns(2)
    cols[0].metric("Balance", f"${summary.current_balance:,.2f}")
    cols[1].metric("Equity", f"${summary.current_equity:,.2f}")
    cols2 = container.columns(2)
    cols2[0].metric("Today realized",
                       f"${summary.realized_pnl_today:+,.2f}")
    cols2[1].metric("Total realized",
                       f"${summary.realized_pnl_total:+,.2f}")


def _add_deployment_form(login: int) -> None:
    strats = discover_strategies()
    with st.expander("➕  Add a deployment", expanded=False):
        with st.form(f"add_dep_{login}"):
            cols = st.columns(3)
            sname = cols[0].selectbox("strategy", sorted(strats.keys()),
                                          key=f"adddep_s_{login}")
            ticker = cols[1].text_input("ticker", value="US100.cash",
                                            key=f"adddep_t_{login}")
            tf = cols[2].selectbox("tf", ["M15", "H1", "H4", "D1"],
                                       index=3, key=f"adddep_tf_{login}")
            cols2 = st.columns(3)
            risk = float(cols2[0].number_input("risk %/trade",
                                                    value=0.3, step=0.1,
                                                    format="%.2f",
                                                    key=f"adddep_r_{login}"))
            cap = float(cols2[1].number_input("daily cap %",
                                                  value=1.0, step=0.5,
                                                  format="%.2f",
                                                  key=f"adddep_c_{login}"))
            long_only = cols2[2].checkbox("long only", value=True,
                                              key=f"adddep_lo_{login}")
            if st.form_submit_button("Add deployment"):
                slug = (Deployment.slug(sname, ticker, tf)
                          + ("_long" if long_only else "_bidir"))
                dep = Deployment(
                    deployment_id=slug,
                    strategy=sname, ticker=ticker, tf=tf,
                    long_only=long_only,
                    params={"long_only": long_only},
                    risk_pct=risk, daily_cap_pct=cap,
                    status="idle",
                )
                dep_mod.upsert_deployment(login, dep)
                st.success(f"Added {slug}")
                st.rerun()


def main():
    st.set_page_config(page_title="Operations", page_icon="🚀", layout="wide")
    st.title("🚀  Operations")
    st.caption(
        "Daily-driver view. Switch accounts at the top, manage deployments + "
        "positions in the cards below. The detailed research views are on "
        "the other pages (Backtest, Strategy Studio, etc.)."
    )

    # 1. Emergency stop bar (sticky-ish at top)
    emergency_stop_bar.render()

    # 2. Account switcher
    account_switcher.render()
    login = get_active_login()
    if login is None:
        st.info("Add an account above to begin.")
        return
    account = account_manager.get_account(login)
    if account is None:
        st.error(f"No account for login {login}")
        return

    _ensure_seed_deployments(login)

    # 3. Account detail + FTMO progress (right column), main content (left)
    main_col, side_col = st.columns([3, 1])

    # Build live components
    bridge = _bridge()
    cfg = load_config()
    tg_cfg = time_guard_cfg_from_risk_config(cfg)
    db_path = account_manager.get_db_path(login)
    clock = _build_clock(account)
    risk_tracker = LiveRiskTracker(
        db_path=db_path,
        max_consecutive_losses=cfg.max_consecutive_losses,
        cooldown_minutes=240,
        daily_loss_cap_pct=cfg.daily_loss_cap_pct,
    )
    parity_gate = ParityGate(db_path)

    # Account info / statement (best-effort — bridge may be offline)
    summary = None
    try:
        statement = StatementReader(
            account_login=login, bridge=bridge,
            ftmo_rules=clock.rules,
            account_baseline=100_000,
        )
        summary = statement.snapshot()
    except Exception as e:
        st.caption(f"(statement unavailable: {e})")

    # Right side
    with side_col:
        _account_card(account, summary, st)
        if summary is not None:
            ftmo_progress.render(
                account_login=login,
                baseline_equity=summary.starting_equity,
                current_equity=summary.current_equity,
                realized_today=summary.realized_pnl_today,
                unrealized=summary.unrealized_pnl,
                container=st,
            )

    # Main content
    with main_col:
        deployments = dep_mod.load_deployments(login)
        st.markdown(f"## Deployments ({len(deployments)})")

        # Modal trigger flag (persisted in session state)
        _SHOW_MODAL_KEY = "_ops_show_go_live_for"
        # Render each card
        for d in deployments:
            def _on_go_live(dep, _login=login):
                st.session_state[_SHOW_MODAL_KEY] = dep.deployment_id

            def _on_paper(dep, _login=login):
                dep_mod.update_status(_login, dep.deployment_id, "paper")
                st.toast(f"📡 {dep.deployment_id} → PAPER")

            def _on_pause(dep, _login=login):
                new_status = "paused" if dep.status != "paused" else "idle"
                dep_mod.update_status(_login, dep.deployment_id, new_status)
                st.toast(f"⏸ {dep.deployment_id} → {new_status.upper()}")

            def _on_remove(dep, _login=login):
                dep_mod.remove_deployment(_login, dep.deployment_id)
                st.toast(f"🗑 removed {dep.deployment_id}")

            def _on_backtest(dep, _login=login):
                # Push to Page 1 via session_state and offer a hint
                st.toast("Open Page 1 (Backtest) — params will be set there.")

            deployment_card.render(
                login=login, dep=d,
                on_go_live=_on_go_live, on_paper=_on_paper,
                on_pause=_on_pause, on_remove=_on_remove,
                on_backtest=_on_backtest,
            )

        _add_deployment_form(login)

        # Go-Live modal (rendered when the flag is set)
        modal_for = st.session_state.get(_SHOW_MODAL_KEY)
        if modal_for is not None:
            target_dep = next((d for d in deployments
                                if d.deployment_id == modal_for), None)
            if target_dep is not None:
                with st.container(border=True):
                    confirmed = go_live_modal.render_modal(
                        login=login, dep=target_dep, cfg=cfg,
                        parity_gate=parity_gate, risk_tracker=risk_tracker,
                        time_guard_cfg=tg_cfg, ftmo_clock=clock,
                        account_baseline=summary.starting_equity if summary else 100_000,
                    )
                    if confirmed:
                        # Final action: in this build the live executor is
                        # mocked at runtime — flip the deployment status
                        # to live and surface a clear message.
                        dep_mod.update_status(login, target_dep.deployment_id, "live")
                        # Audit
                        try:
                            JournalWriter(db_path).record_bridge_event(
                                method="ui_go_live_confirmed",
                                latency_ms=0, ok=True,
                                error=f"deployment={target_dep.deployment_id}",
                            )
                        except Exception:
                            pass
                        st.session_state.pop(_SHOW_MODAL_KEY, None)
                        st.toast(f"🚀 LIVE: {target_dep.deployment_id}")
                        st.rerun()

        # Position manager panel
        st.markdown("---")
        try:
            pm = PositionManager(account_login=login, bridge=bridge,
                                  db_path=db_path)
            position_manager_panel.render(pm=pm)
        except Exception as e:
            st.caption(f"(position manager unavailable: {e})")

        # Statement panel
        if summary is not None:
            st.markdown("---")
            statement_panel.render(summary=summary, clock=clock,
                                     user_tz_name=account.user_tz)

        # Equity history chart (broker-truth)
        if summary is not None:
            st.markdown("---")
            try:
                curve = statement.daily_pnl_curve(days=30)
                live_equity_chart.render(daily_curve=curve,
                                            baseline_equity=summary.starting_equity)
            except Exception as e:
                st.caption(f"(equity chart unavailable: {e})")


main()
