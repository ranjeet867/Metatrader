"""
0_🚀_Operations.py — the trader's front door.

Layout (top→bottom):
  1. Emergency-stop banner (always visible)
  2. Account switcher row (compact)
  3. KPI strip (8 cells, full width — balance / equity / unrealized /
     today / daily-buffer / total-buffer / profit-progress / next FTMO close)
  4. Tabs:  📊 Deployments | 💼 Positions | 📜 Statement | 📈 Equity | 🛠 Settings
  5. Add-deployment expander, Go-Live modal, etc.
"""
from __future__ import annotations

import sys
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
    active_runs_panel,
    activity_log_panel,
    deployment_card,
    deployment_dialogs,
    emergency_stop_bar,
    kpi_strip,
    live_equity_chart,
    position_manager_panel,
    statement_panel,
    theme,
)
from dashboards.components.account_switcher import (   # noqa: E402
    get_active_login,
)
from dashboards.components.state import (   # noqa: E402
    discover_strategies,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    if not dep_mod.load_deployments(login):
        dep_mod.seed_survivor_deployments(login)


def _add_deployment_form(login: int) -> None:
    strats = discover_strategies()
    with st.expander("➕  Add a deployment", expanded=False):
        with st.form(f"add_dep_{login}", clear_on_submit=True):
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


def _account_meta_row(account: account_manager.Account,
                       summary, login: int) -> None:
    """A compact row beneath the account switcher: alias · login · type ·
    timezone · 'reset baseline' tool when the account is past its loss
    limit."""
    cols = st.columns([5, 2])
    cols[0].markdown(
        f"<span style='color:#9ca3af;font-family:ui-monospace,Menlo,monospace;"
        f"font-size:0.86rem;'>"
        f"<b style='color:#e5e7eb;'>{account.alias}</b> · "
        f"login <code>{account.login}</code> · type <code>{account.type}</code>"
        f" · tz <code>{account.user_tz}</code></span>",
        unsafe_allow_html=True,
    )
    # Offer "reset baseline" if the account has crossed FTMO total-loss
    # cap. Useful for re-anchoring sandboxed/already-blown accounts.
    if summary is not None:
        baseline = account.effective_baseline_equity
        cap_dollars = baseline * account.total_loss_cap_pct / 100.0
        loss = max(0.0, baseline - summary.current_equity)
        if loss >= cap_dollars * 0.9 and summary.current_equity > 0:
            with cols[1].popover("⚠ Re-anchor baseline"):
                st.warning(
                    f"This account is at {loss/cap_dollars*100:.0f}% of its "
                    f"FTMO total-loss cap relative to baseline "
                    f"${baseline:,.0f}. If you're using it as a sandbox, "
                    f"set the baseline to current equity to make the buffer "
                    f"math meaningful again."
                )
                if st.button(
                    f"Set baseline = ${summary.current_equity:,.0f}",
                    type="primary", key=f"reset_baseline_{login}",
                ):
                    account_manager.reset_baseline_to_current(
                        login, summary.current_equity)
                    st.toast("Baseline reset.")
                    st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Operations", page_icon="🚀",
                       layout="wide")
    theme.inject_css()

    # 1. Emergency stop banner
    emergency_stop_bar.render()

    # 2. Account switcher
    account_switcher.render()
    login = get_active_login()
    if login is None:
        st.info("Add an account above to begin.")
        return
    account = account_manager.get_account(login)
    if account is None:
        st.error(f"No account record for login {login}")
        return

    _ensure_seed_deployments(login)

    # 3. Build the live components
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

    # 4. Statement snapshot (best-effort — bridge may be offline)
    summary = None
    statement = None
    statement_err = None
    try:
        statement = StatementReader(
            account_login=login, bridge=bridge,
            ftmo_rules=clock.rules,
            account_baseline=account.effective_baseline_equity,
            daily_loss_cap_pct=account.daily_loss_cap_pct,
            total_loss_cap_pct=account.total_loss_cap_pct,
        )
        summary = statement.snapshot()
    except Exception as e:
        statement_err = str(e)

    _account_meta_row(account, summary, login)

    # 5. KPI strip (full width)
    kpi_strip.render(account=account, summary=summary, clock=clock)

    if summary is None:
        # Differentiate offline-bridge vs old-bridge-EA. The latter is
        # common when the user hasn't upgraded their MT5 EA yet.
        msg = statement_err or ""
        if "unknown method" in msg or "not implemented" in msg.lower():
            st.warning(
                "ℹ Your MT5 bridge EA is missing Phase 2.5 methods "
                "(`positions_get`, `history_deals_get`). Live KPIs are "
                "showing baseline only. Upgrade the EA when convenient — "
                "see `docs/RUNBOOK.md`."
            )
        else:
            st.caption(
                f"⚠ Bridge offline — KPIs show baseline / countdowns only. "
                f"({msg or 'no error'})"
            )

    st.markdown("")  # vertical breathing room

    # 6. Tabs
    tab_dep, tab_runs, tab_pos, tab_stmt, tab_eq, tab_log, tab_settings = (
        st.tabs([
            "📊 Deployments", "▶ Active runs", "💼 Positions",
            "📜 Statement", "📈 Equity history",
            "📜 Activity log", "🛠 Settings",
        ])
    )

    # ----- Deployments tab -----
    with tab_dep:
        deployments = dep_mod.load_deployments(login)
        st.markdown(
            f"**{len(deployments)} deployments configured** for this account."
        )

        for d in deployments:
            def _on_backtest(dep, _login=login):
                deployment_dialogs.open_dialog("backtest", dep.deployment_id)

            def _on_paper(dep, _login=login):
                deployment_dialogs.open_dialog("paper", dep.deployment_id)

            def _on_go_live(dep, _login=login):
                deployment_dialogs.open_dialog("go_live", dep.deployment_id)

            def _on_pause(dep, _login=login):
                # Pause is fast — no dialog; just toggle and toast.
                new_status = "paused" if dep.status != "paused" else "idle"
                dep_mod.update_status(_login, dep.deployment_id, new_status)
                # Audit so the activity log shows it
                try:
                    JournalWriter(db_path).record_bridge_event(
                        method=("pause_deployment"
                                  if new_status == "paused"
                                  else "resume_deployment"),
                        latency_ms=0, ok=True,
                        error=f"deployment={dep.deployment_id}",
                    )
                except Exception:
                    pass
                st.toast(f"{'⏸' if new_status == 'paused' else '▶'} "
                            f"{dep.deployment_id} → {new_status.upper()}")
                st.rerun()

            def _on_remove(dep, _login=login):
                deployment_dialogs.open_dialog("remove", dep.deployment_id)

            deployment_card.render(
                login=login, dep=d,
                on_go_live=_on_go_live, on_paper=_on_paper,
                on_pause=_on_pause, on_remove=_on_remove,
                on_backtest=_on_backtest,
            )

        _add_deployment_form(login)

        # Render whichever dialog was requested by a button click.
        deployment_dialogs.render_active_dialog(
            deployments=deployments, login=login,
            cfg=cfg, parity_gate=parity_gate,
            risk_tracker=risk_tracker, time_guard_cfg=tg_cfg,
            ftmo_clock=clock,
            account_baseline=(
                summary.starting_equity if summary
                else account.effective_baseline_equity),
        )

    # ----- Active runs tab -----
    with tab_runs:
        active_runs_panel.render(login=login)

    # ----- Positions tab -----
    with tab_pos:
        try:
            pm = PositionManager(account_login=login, bridge=bridge,
                                 db_path=db_path)
            position_manager_panel.render(pm=pm)
        except Exception as e:
            st.caption(f"(position manager unavailable: {e})")

    # ----- Statement tab -----
    with tab_stmt:
        if summary is None:
            st.info("Bridge offline — statement unavailable.")
        else:
            statement_panel.render(summary=summary, clock=clock,
                                     user_tz_name=account.user_tz,
                                     account_type=account.type,
                                     total_loss_cap_pct=account.total_loss_cap_pct)

    # ----- Equity history tab -----
    with tab_eq:
        if statement is None or summary is None:
            st.info("Bridge offline — equity history unavailable.")
        else:
            try:
                curve = statement.daily_pnl_curve(days=60)
                live_equity_chart.render(
                    daily_curve=curve,
                    baseline_equity=summary.starting_equity,
                )
            except Exception as e:
                st.caption(f"(equity chart unavailable: {e})")

    # ----- Activity log tab -----
    with tab_log:
        activity_log_panel.render(login=login, hours=24)

    # ----- Settings tab -----
    with tab_settings:
        st.markdown("### Per-account settings")
        with st.form(f"acct_settings_{login}"):
            cols = st.columns(2)
            new_alias = cols[0].text_input("alias", value=account.alias)
            new_tz = cols[1].text_input("timezone (IANA)", value=account.user_tz)
            cols2 = st.columns(3)
            new_baseline = float(cols2[0].number_input(
                "Risk baseline equity ($)",
                value=float(account.effective_baseline_equity),
                step=1_000.0, min_value=0.0,
                help="0 to infer from type. Used for FTMO buffer math.",
            ))
            new_daily_cap = float(cols2[1].number_input(
                "Daily loss cap (%)",
                value=float(account.daily_loss_cap_pct),
                step=0.5, min_value=0.5, max_value=10.0,
            ))
            new_total_cap = float(cols2[2].number_input(
                "Total loss cap (%)",
                value=float(account.total_loss_cap_pct),
                step=0.5, min_value=1.0, max_value=20.0,
            ))
            cols3 = st.columns(2)
            new_profit_target = float(cols3[0].number_input(
                "Profit target (%)",
                value=float(account.profit_target_pct),
                step=0.5, min_value=1.0, max_value=20.0,
            ))
            new_days_required = int(cols3[1].number_input(
                "Min trading days",
                value=int(account.days_required),
                step=1, min_value=0, max_value=60,
            ))
            if st.form_submit_button("Save settings", type="primary"):
                account_manager.update_account(
                    login,
                    alias=new_alias, user_tz=new_tz,
                    risk_baseline_equity=new_baseline,
                    daily_loss_cap_pct=new_daily_cap,
                    total_loss_cap_pct=new_total_cap,
                    profit_target_pct=new_profit_target,
                    days_required=new_days_required,
                )
                st.success("Saved.")
                st.rerun()

        st.markdown("---")
        st.markdown("### Danger zone")
        if st.button("🗑 Remove this account from registry",
                      help="Does NOT delete the data/accounts/{login}/ folder."):
            account_manager.remove_account(login)
            st.session_state.pop("_active_account_login", None)
            st.toast(f"Removed account {login}.")
            st.rerun()


main()
