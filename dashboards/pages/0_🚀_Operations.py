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
    system_status_panel,
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


def _open_positions_snapshot(login: int, *, deployments=None,
                                summary=None) -> list:
    """Build a list of OpenPosition for the system_status_panel.

    Source order:
      1. summary.open_positions (from StatementReader) if populated
      2. Empty list otherwise — panel renders, just without collisions

    The list ties each open position to the deployment_id that
    presumably owns it. We match by symbol; if multiple deployments
    trade the same symbol the collision check will surface them.
    """
    from core.position_guard import OpenPosition

    if summary is None or not getattr(summary, "open_positions", None):
        return []
    if deployments is None:
        try:
            deployments = dep_mod.load_deployments(login)
        except Exception:
            deployments = []

    # Build a (symbol → first-deployment-id) map for ownership inference
    sym_to_dep: dict[str, str] = {}
    for d in deployments:
        sym_to_dep.setdefault(d.ticker, d.deployment_id)

    out = []
    for p in summary.open_positions:
        try:
            sym = getattr(p, "symbol", None) or p.get("symbol")
            side = getattr(p, "type", None) or p.get("type", "") or "LONG"
            lots = float(getattr(p, "volume", 0.0)
                            or p.get("volume", 0.0) or 0.0)
            opened = (str(getattr(p, "opened_at_utc", "")
                          or p.get("opened_at_utc", "") or ""))
        except Exception:
            continue
        if not sym:
            continue
        out.append(OpenPosition(
            deployment_id=sym_to_dep.get(sym, f"unknown:{sym}"),
            symbol=sym,
            side=str(side).upper(),
            lots=lots,
            opened_at_utc=opened,
        ))
    return out


def _build_clock(account: account_manager.Account) -> FtmoClock:
    try:
        tz = ZoneInfo(account.user_tz)
    except Exception:
        tz = ZoneInfo("UTC")
    return FtmoClock(FtmoRules(user_tz=tz))


def _ensure_seed_deployments(login: int) -> None:
    """Seed the recommended portfolio ONCE per account, never again.

    Pre-fix this re-seeded every time `load_deployments(login)` came
    back empty — so a user who deleted every deployment (e.g. to start
    fresh from the Composer's Recommended preset) saw the same 6 cells
    pop back on the next render. The remove button looked broken.

    The marker file `.seeded` records that the initial seed has already
    happened. Once written, this function becomes a no-op forever for
    that account, regardless of how many deployments exist.

    To force a re-seed: delete `data/accounts/<login>/.seeded` then
    reload the Operations page."""
    db_path = account_manager.get_db_path(login)
    marker = db_path.parent / ".seeded"
    if marker.exists():
        return
    if not dep_mod.load_deployments(login):
        dep_mod.seed_survivor_deployments(login)
    # Always write the marker, even if the user already had deployments
    # before we reached this code path. Never auto-seed this account
    # again, period.
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"Seeded at first Operations-page render. "
            f"Delete this file to force a re-seed.\n"
        )
    except OSError:
        pass


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

    # 5b. System status (circuit breaker + position-collision view)
    # Build the open-positions snapshot from the bridge if it's reachable;
    # falls back to an empty list when bridge is offline so the panel
    # still renders (showing only realised PnL).
    open_positions_snapshot = _open_positions_snapshot(
        login, deployments=None, summary=summary,
    )
    try:
        system_status_panel.render(
            login=login, db_path=db_path, mode="live",
            open_positions=open_positions_snapshot,
        )
    except Exception as e:        # pragma: no cover — defensive
        st.caption(f"⚠ system-status panel error: {e}")

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
        # Pull broker positions ONCE for all cards so the holding-badge
        # can show 💼 LONG @ entry · +$X inline without N bridge calls.
        broker_positions_for_cards = []
        try:
            from core.mt5_account import MT5AccountClient
            broker_positions_for_cards = MT5AccountClient().positions_get()
        except Exception:
            pass
        # Header summary so the user sees the holding/flat split at a glance
        from dashboards.components.holding_badge import summarize as _hb_sum
        sm = _hb_sum(deployments, broker_positions_for_cards)
        st.markdown(
            f"**{len(deployments)} deployments configured** for this account."
            f"  ·  💼 {sm['n_holding']} holding"
            f"  ·  ⚪ {sm['n_flat']} flat"
            + (f"  ·  ⛔ {sm['n_halted']} halted"
               if sm['n_halted'] else "")
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
                broker_positions=broker_positions_for_cards,
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
        except AttributeError as e:
            # Almost certainly a stale-class issue — Streamlit's hot
            # reloader doesn't re-init dataclasses on edit. Tell the
            # user exactly what to do.
            st.error(
                f"⛔ **Position manager: stale-class error** — "
                f"`{e}`\n\n"
                f"This means Streamlit is running an older version of "
                f"a dataclass than the one on disk. Streamlit's "
                f"hot-reloader doesn't pick up new fields/properties "
                f"on dataclasses already loaded into memory.\n\n"
                f"**Fix:** stop the dashboard (Ctrl+C in the terminal) "
                f"and restart with `make dashboard`. Everything will "
                f"work after a clean restart."
            )
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
        # Phase-32 UI consolidation: account record + circuit breaker
        # editors are now ALSO available on the unified ⚙️ Settings
        # page (sidebar). The forms here remain functional and edit
        # the same files, so values stay consistent — but the ⚙️
        # Settings page is the canonical home going forward.
        st.warning(
            "⚙️ **These same settings are now also editable on the "
            "[⚙️ Settings](/Settings) page** (sidebar). Both pages "
            "edit the same files (`risk_config.json`, "
            "`circuit_breaker.json`, account registry) — values stay "
            "consistent. Use whichever feels easier; future cleanup "
            "will remove the duplicates here.",
            icon="ℹ️",
        )
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
        st.markdown("### 🛡️  Circuit breaker")
        st.caption(
            "System-wide stop-loss / target halt. When the runner sees "
            "any of these thresholds breached on this account, it "
            "REFUSES new opens. HALT-level breaches also auto-pause "
            "every live deployment so a runaway can't compound."
        )
        from core import circuit_breaker as cb
        cb_cfg = cb.load_config(login)
        with st.form(f"cb_settings_{login}"):
            cb_cols = st.columns(3)
            cb_dl = float(cb_cols[0].number_input(
                "Daily loss limit ($)",
                value=float(cb_cfg.daily_loss_dollars or 0.0),
                step=100.0, min_value=0.0,
                help="HALT if today's realised loss ≥ this. 0 = disabled.",
            ))
            cb_tl = float(cb_cols[1].number_input(
                "Total loss limit ($)",
                value=float(cb_cfg.total_loss_dollars or 0.0),
                step=100.0, min_value=0.0,
                help="HALT if lifetime realised loss ≥ this. 0 = disabled.",
            ))
            cb_dt = float(cb_cols[2].number_input(
                "Daily target ($)",
                value=float(cb_cfg.daily_target_dollars or 0.0),
                step=100.0, min_value=0.0,
                help="STOP_NEW (lock gains) when today's realised gain "
                      "≥ this. 0 = disabled.",
            ))
            cb_cols2 = st.columns(3)
            cb_tt = float(cb_cols2[0].number_input(
                "Total target ($)",
                value=float(cb_cfg.total_target_dollars or 0.0),
                step=100.0, min_value=0.0,
                help="STOP_NEW when lifetime realised gain ≥ this "
                      "(e.g. FTMO 8% profit target). 0 = disabled.",
            ))
            cb_cl = int(cb_cols2[1].number_input(
                "Max consec losses",
                value=int(cb_cfg.max_consec_losses or 0),
                step=1, min_value=0, max_value=20,
                help="HALT after this many losing trades in a row. "
                      "0 = disabled.",
            ))
            cb_op = int(cb_cols2[2].number_input(
                "Max open positions",
                value=int(cb_cfg.max_open_positions or 0),
                step=1, min_value=0, max_value=50,
                help="STOP_NEW once this many positions are open across "
                      "all deployments. 0 = disabled.",
            ))
            cb_halt = st.checkbox(
                "Auto-pause live deployments on HALT",
                value=cb_cfg.halt_on_breach,
                help="When ON and a HALT condition fires, every "
                      "`live`-status deployment is flipped to `halted`. "
                      "Paper deployments are left alone.",
            )
            if st.form_submit_button("Save circuit-breaker config",
                                          type="primary"):
                new_cb_cfg = cb.CircuitConfig(
                    daily_loss_dollars=cb_dl or None,
                    total_loss_dollars=cb_tl or None,
                    daily_target_dollars=cb_dt or None,
                    total_target_dollars=cb_tt or None,
                    max_consec_losses=cb_cl or None,
                    max_open_positions=cb_op or None,
                    halt_on_breach=cb_halt,
                )
                cb.save_config(login, new_cb_cfg)
                st.success("Circuit-breaker config saved.")
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
