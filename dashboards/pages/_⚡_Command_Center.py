"""
_⚡_Command_Center.py — single-page system status + emergency controls.

Underscore prefix puts this page FIRST in Streamlit's sidebar so it's
the natural landing page. Answers every "is the system healthy right
now?" question at a glance.

Sections:
  1. ⛔ EMERGENCY STOP — close all + halt + kill runner (one click + confirm)
  2. Header strip — runner heartbeat, bridge latency, equity, holding,
     circuit breaker, last trade
  3. Component health grid — MT5 process, App Nap, LaunchAgents,
     symbol_info coverage, parity coverage, quality violations,
     position-guard collisions
  4. Live deployments — count of holding/flat/halted
  5. Live activity feed — last 50 bridge events
  6. Runner log tail — last 30 lines from /tmp/mt5_runner.log
  7. Quick actions — refresh symbol info, run preflight, restart runner,
     run all stale parity
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import account_manager, deployment as dep_mod   # noqa: E402
from dashboards.components import theme                  # noqa: E402

DB_PATH = REPO / "data" / "v2.db"
RUNNER_LOG = Path("/tmp/mt5_runner.log")
RUNNER_ERR = Path("/tmp/mt5_runner.err")
RUNNER_PLIST = Path.home() / "Library/LaunchAgents/com.user.mt5_runner.plist"
KEEP_MT5_PLIST = Path.home() / "Library/LaunchAgents/com.user.keep_mt5_alive.plist"


# ─────────────────────────────────────────────────────────────────────
# Health probes
# ─────────────────────────────────────────────────────────────────────

def _runner_heartbeat(login: int) -> tuple[str, str]:
    """Returns (state, detail) — state ∈ {ok, stale, missing}."""
    db_path = account_manager.get_db_path(login)
    state_file = db_path.parent / "runner_state.json"
    if not state_file.exists():
        return "missing", "runner_state.json not found — never run"
    try:
        data = json.loads(state_file.read_text())
        saved_at = data.get("saved_at_utc")
        if saved_at is None:
            return "missing", "no saved_at_utc field"
        saved_dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
        age_s = (datetime.now(timezone.utc) - saved_dt).total_seconds()
        if age_s > 300:   # 5 min
            return "stale", f"{age_s/60:.0f} min ago"
        return "ok", f"{age_s:.0f}s ago"
    except Exception as e:
        return "missing", f"corrupt: {e}"


def _bridge_status() -> tuple[str, str, dict | None]:
    """Returns (state, detail, info_dict)."""
    try:
        from core.mt5_account import MT5AccountClient
        client = MT5AccountClient()
        t0 = time.time()
        info = client.account_info(force_refresh=True)
        ms = int((time.time() - t0) * 1000)
        if info.equity <= 0:
            return "down", "equity = 0", None
        return "ok", f"{ms}ms · equity ${info.equity:,.2f}", {
            "balance": info.balance, "equity": info.equity,
            "margin": info.margin, "margin_free": info.margin_free,
        }
    except Exception as e:
        return "down", f"{type(e).__name__}", None


def _mt5_process_status() -> tuple[str, str]:
    """Is MetaTrader 5 process running on macOS?"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "MetaTrader 5"],
            capture_output=True, text=True, timeout=2,
        )
        if result.stdout.strip():
            pid = result.stdout.strip().split()[0]
            return "ok", f"pid {pid}"
        return "down", "not running"
    except Exception:
        return "unknown", "pgrep failed"


def _app_nap_disabled() -> tuple[str, str]:
    """Is App Nap disabled for MT5?"""
    try:
        result = subprocess.run(
            ["defaults", "read", "com.metaquotes.MetaTrader.5",
             "NSAppSleepDisabled"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and "1" in result.stdout:
            return "ok", "disabled"
        return "warn", "App Nap may suspend MT5"
    except Exception:
        return "unknown", "defaults read failed"


def _launchagent_status(plist_path: Path, label: str) -> tuple[str, str]:
    """Returns (ok|warn|missing, detail) for a LaunchAgent plist."""
    if not plist_path.exists():
        return "missing", "not installed"
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.splitlines():
            if label in line:
                pid = line.split("\t")[0]
                if pid == "-":
                    return "warn", "loaded but not running (last exit abnormal)"
                return "ok", f"running · pid {pid}"
        return "warn", "plist present but not loaded"
    except Exception as e:
        return "unknown", str(e)


def _symbol_info_coverage(deployments) -> tuple[str, str, list]:
    """Returns (state, summary, list_of_missing_tickers)."""
    from core.symbol_info_loader import try_load
    needed = sorted({d.ticker for d in deployments
                       if d.status in ("paper", "live")})
    missing = [t for t in needed if try_load(t) is None]
    if not needed:
        return "ok", "no live/paper deps", []
    if not missing:
        return "ok", f"{len(needed)}/{len(needed)} covered", []
    return "warn", f"{len(needed) - len(missing)}/{len(needed)} covered", missing


def _parity_coverage(deployments) -> tuple[str, str, list]:
    """Returns (state, summary, list_of_stale_or_never)."""
    from core.parity_gate import ParityGate
    from dashboards.components.state import discover_strategies
    from dashboards.components.strategy_resolver import resolve_base_strategy
    gate = ParityGate(DB_PATH)
    strats = discover_strategies()
    bases_seen = set()
    stale = []
    fresh_count = 0
    for d in deployments:
        if d.status not in ("paper", "live"):
            continue
        base = resolve_base_strategy(d.strategy, strats) or d.strategy
        if base in bases_seen:
            continue
        bases_seen.add(base)
        if gate.is_recent(base):
            fresh_count += 1
        else:
            stale.append(base)
    if not bases_seen:
        return "ok", "no live/paper deps", []
    if not stale:
        return "ok", f"{fresh_count}/{len(bases_seen)} fresh", []
    return "warn", f"{fresh_count}/{len(bases_seen)} fresh", stale


def _circuit_breaker_status(login: int, n_open: int) -> tuple[str, str, list]:
    """Returns (state, detail, reasons)."""
    try:
        from core import circuit_breaker as cb
        cfg = cb.load_config(login)
        s = cb.evaluate(
            DB_PATH, mode="live", cfg=cfg,
            open_positions_count=n_open,
        )
        if s.state == "OK":
            return "ok", (
                f"today ${s.realised_today:+,.0f} · "
                f"total ${s.realised_total:+,.0f} · "
                f"streak {s.consec_losses}"
            ), []
        if s.state == "STOP_NEW":
            return "warn", "STOP_NEW — locking gains", s.reasons
        return "halt", "HALT — refusing all signals", s.reasons
    except Exception as e:
        return "unknown", f"{type(e).__name__}: {e}", []


# ─────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────

_STATE_COLOR = {
    "ok":      ("#16a34a", "✅"),
    "warn":    ("#f59e0b", "🟡"),
    "halt":    ("#dc2626", "⛔"),
    "down":    ("#dc2626", "⛔"),
    "missing": ("#dc2626", "⛔"),
    "stale":   ("#f59e0b", "🟡"),
    "unknown": ("#6b7280", "❓"),
}


def _tile(label: str, state: str, detail: str,
            container=None) -> None:
    target = container or st
    color, icon = _STATE_COLOR.get(state, _STATE_COLOR["unknown"])
    target.markdown(
        f"<div style='border:1px solid {color}40;background:{color}15;"
        f"padding:0.6rem 0.9rem;border-radius:6px;'>"
        f"<div style='font-size:0.72rem;color:#9ca3af;"
        f"text-transform:uppercase;letter-spacing:0.06em;'>{label}</div>"
        f"<div style='font-size:1.05rem;color:{color};font-weight:700;"
        f"line-height:1.3;'>{icon} {state.upper()}</div>"
        f"<div style='font-size:0.78rem;color:#cbd5e1;'>{detail}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_edge_decay_tile(login: int, deployments) -> None:
    """Live edge-decay state across deployments.

    For every deployment with closed trades, compute the rolling z-score
    of live R vs catalog R. Tile shows total counts in each state plus
    a per-deployment expandable breakdown.

    Auto-demotion is enforced inside DeploymentRunner; this tile is the
    user-facing surface that explains WHY a cell flipped or is about to.
    """
    from core import edge_decay
    from core import edge_catalog as _ec

    # Build {deployment_id: catalog_R} for assess_all
    catalog_R_map: dict[str, float] = {}
    for d in deployments:
        try:
            edge_row = _ec.best_for(d.ticker, d.tf, d.strategy)
            if edge_row is None:
                continue
            cat_R = float(
                getattr(edge_row, "expectancy_per_R", 0.0)
                or getattr(edge_row, "test_r", 0.0) or 0.0
            )
            catalog_R_map[d.deployment_id] = cat_R
        except Exception:
            continue

    db_path = REPO / "data" / "v2.db"
    try:
        assessments = edge_decay.assess_all(db_path, catalog_R_map)
    except Exception:
        assessments = []

    # Count by state
    counts = {s: 0 for s in (edge_decay.DecayState.BLUE,
                              edge_decay.DecayState.GREEN,
                              edge_decay.DecayState.YELLOW,
                              edge_decay.DecayState.RED,
                              edge_decay.DecayState.INSUFFICIENT)}
    for a in assessments:
        counts[a.state] = counts.get(a.state, 0) + 1

    n_red = counts[edge_decay.DecayState.RED]
    n_yellow = counts[edge_decay.DecayState.YELLOW]
    n_blue = counts[edge_decay.DecayState.BLUE]
    n_green = counts[edge_decay.DecayState.GREEN]
    n_insuf = counts[edge_decay.DecayState.INSUFFICIENT]

    # Headline color reflects the worst case
    if n_red > 0:
        headline_emoji = "🔴"
        headline = f"{n_red} cell(s) auto-demoted by edge-decay"
        bg = "#7f1d1d"
    elif n_yellow > 0:
        headline_emoji = "🟡"
        headline = f"{n_yellow} cell(s) underperforming catalog"
        bg = "#854d0e"
    elif n_blue > 0:
        headline_emoji = "🔵"
        headline = f"{n_blue} cell(s) over-performing catalog"
        bg = "#1e3a8a"
    elif n_green > 0:
        headline_emoji = "🟢"
        headline = f"{n_green} cell(s) tracking catalog within noise"
        bg = "#14532d"
    else:
        headline_emoji = "⚪"
        headline = "No closed trades yet — assessing in INSUFFICIENT state"
        bg = "#374151"

    with st.container(border=True):
        st.markdown(
            f"<div style='background:{bg};padding:10px 16px;"
            f"border-radius:6px;color:white;font-weight:600;"
            f"font-size:0.95rem;'>{headline_emoji} Edge decay: "
            f"{headline}</div>",
            unsafe_allow_html=True,
        )
        cols = st.columns(5)
        cols[0].metric("🔴 RED",      n_red,
                          help="Auto-demoted: live R > 2σ below catalog "
                                "for ≥15 consecutive trades")
        cols[1].metric("🟡 YELLOW",  n_yellow,
                          help="Live R 1-2σ below catalog — investigate, "
                                "no auto-action yet")
        cols[2].metric("🟢 GREEN",   n_green,
                          help="Live R within noise of catalog — healthy")
        cols[3].metric("🔵 BLUE",    n_blue,
                          help="Live R materially ABOVE catalog — "
                                "consider sizing up")
        cols[4].metric("⚪ Pending", n_insuf,
                          help=f"Fewer than {edge_decay.MIN_TRADES_TO_ASSESS} "
                                "closed trades — collecting data")

        # Per-deployment detail (collapsed by default)
        if assessments:
            with st.expander(
                "Per-deployment z-score breakdown",
                expanded=(n_red > 0 or n_yellow > 0),
            ):
                rows = []
                for a in assessments:
                    rows.append({
                        "Deployment":   a.deployment_id,
                        "State":        a.state.value.upper(),
                        "n_trades":     a.n_trades,
                        "Live R":       (round(a.rolling_mean_R, 3)
                                          if a.rolling_mean_R is not None
                                          else float("nan")),
                        "Catalog R":    round(a.catalog_R, 3),
                        "z-score":      (round(a.z_score, 2)
                                            if a.z_score is not None
                                            else float("nan")),
                        "Sustained red": a.sustained_red_trades,
                        "Reason":       a.reason,
                    })
                import pandas as pd
                df = pd.DataFrame(rows)
                df = df.sort_values(
                    by="z-score", ascending=True, na_position="last")
                st.dataframe(
                    df, width="stretch", hide_index=True,
                    column_config={
                        "Live R":     st.column_config.NumberColumn(
                            "Live R", format="%.3f"),
                        "Catalog R":  st.column_config.NumberColumn(
                            "Catalog R", format="%.3f"),
                        "z-score":    st.column_config.NumberColumn(
                            "z-score", format="%.2f"),
                    },
                )
        st.caption(
            "💡 Edge-decay autolearner — rolling z-score of live R vs "
            "catalog `expectancy_per_R`. Auto-demote fires on RED "
            "(z<-2 sustained 15 trades). Reset by recording new trades; "
            "no manual intervention needed."
        )


def _render_daily_risk_budget_tile(login: int) -> None:
    """Show today's adaptive-risk budget: P&L vs cap, buffer remaining,
    next per-trade size, equity regime. Mirrors what the runner sees
    when computing risk each tick.

    Pre-fix the user had to grep `/tmp/mt5_runner.log` for
    `adaptive_risk` lines to know what risk size was being used. This
    tile surfaces it on screen, refreshing on page reload.
    """
    from core import (   # noqa: E402
        adaptive_risk as _ar,
        daily_pnl as _dpnl,
        equity_tracker as _et,
        system_config as _sc,
    )

    cfg = _sc.load_system_config(login)
    db_path = account_manager.get_db_path(login)

    use_adaptive = cfg.account_risk.use_adaptive_risk
    if not use_adaptive:
        st.info(
            "ℹ️ **Adaptive risk auto-mode is OFF.** Using static "
            "per-deployment `risk_pct`. Enable on the **⚙️ Settings** "
            "page to scale risk with remaining FTMO buffer + recovery "
            "pattern.",
            icon="ℹ️",
        )
        return

    # Pull live state
    starting_balance = cfg.cost_model.starting_balance_usd
    today_pnl = _dpnl.realized_pnl_today(
        db_path, rollover_hhmm=cfg.account_risk.ftmo_daily_reset_utc,
    )
    # Try broker for live equity; fall back to baseline
    try:
        from core.mt5_account import MT5AccountClient
        from core.mt5_bridge import MT5FileBridge
        bridge = MT5FileBridge()
        client = MT5AccountClient(bridge_call=bridge._call)
        info = client.account_info()
        current_equity = float(info.equity)
    except Exception:
        current_equity = starting_balance + today_pnl

    ar_result = _ar.calculate(
        current_equity=current_equity,
        baseline_equity=starting_balance,
        daily_pnl_so_far=today_pnl,
        daily_soft_cap_pct=cfg.account_risk.daily_soft_cap_pct,
        daily_hard_cap_pct=cfg.account_risk.daily_loss_cap_pct,
        total_loss_floor_pct=10.0,
        target_trades_per_day=cfg.account_risk.target_trades_per_day,
        max_risk_pct=1.0,
        min_risk_pct=0.05,
    )
    regime = _et.assess_now(db_path, baseline_equity=starting_balance)

    st.markdown("### 🎯  Today's risk budget (live)")
    st.caption(
        "Computed from current broker equity + today's realized P&L "
        "+ FTMO buffer math. Refreshes on page reload. The runner uses "
        "these exact numbers on its next tick."
    )

    cols = st.columns(5)
    color_emoji = {
        "daily_soft_cap": "🟡",
        "daily_hard_cap": "🟠",
        "total_loss_floor": "🔴",
        "max_risk_clamp": "🟢",
        "none": "🛑",
    }
    binding_label = (
        f"{color_emoji.get(ar_result.binding_constraint.value, '⚪')} "
        f"{ar_result.binding_constraint.value}"
    )
    with cols[0]:
        st.metric("Per-trade risk",
                   f"{ar_result.risk_pct:.2f}%",
                   help=f"raw before clamp: {ar_result.raw_risk_pct:.3f}%")
        st.caption(f"= ${ar_result.per_trade_risk_usd:.0f}/trade")
    with cols[1]:
        st.metric("Buffer remaining today",
                   f"${ar_result.effective_buffer_usd:.0f}",
                   help="Smaller of: daily-soft, daily-hard, "
                        "total-floor buffers.")
        st.caption(binding_label)
    with cols[2]:
        st.metric("Trades left today",
                   ar_result.max_trades_today_remaining)
        st.caption(f"target: {cfg.account_risk.target_trades_per_day}/day")
    with cols[3]:
        delta_str = (
            f"${today_pnl:+,.0f}" if today_pnl != 0 else "$0"
        )
        st.metric("Today's P&L (realized)", delta_str,
                   delta=delta_str if today_pnl < 0 else None,
                   delta_color=("inverse" if today_pnl < 0 else "normal"))
        soft_cap_usd = (starting_balance
                          * cfg.account_risk.daily_soft_cap_pct / 100)
        st.caption(f"soft cap: -${soft_cap_usd:.0f} "
                    f"({cfg.account_risk.daily_soft_cap_pct:.1f}%)")
    with cols[4]:
        regime_emoji = {
            "normal": "🟢",
            "fragile_recovery": "⚠️",
            "stable_peak": "🚀",
        }.get(regime.regime.value, "⚪")
        st.metric("Equity regime",
                   f"{regime_emoji} {regime.regime.value}")
        st.caption(f"factor: {regime.recovery_factor:.1f}× "
                    f"({regime.n_samples} samples)")

    if not ar_result.allow_trade:
        st.error(
            f"🛑 **Trades BLOCKED** — {ar_result.reason}",
            icon="🛑",
        )
    elif ar_result.binding_constraint.value == "total_loss_floor":
        st.warning(
            f"⚠️ Total-loss floor is the binding constraint — account "
            f"is close to the FTMO 10% breach. Consider pausing.",
            icon="⚠️",
        )


def _emergency_stop_panel(login: int) -> None:
    """Toggle-style ON/OFF emergency stop with optional close-all + kill."""
    e_active = account_manager.emergency_stop_active()

    if e_active:
        # ON state — big red banner + one-click OFF (no typed confirm)
        with st.container(border=True):
            cols = st.columns([4, 1])
            cols[0].markdown(
                "## ⛔ EMERGENCY STOP — **ACTIVE**\n"
                "🟥 Live executor is refusing every `send_order`. No new "
                "trades will fire on any deployment regardless of status. "
                "Open positions on the broker are NOT auto-closed by "
                "this flag — close manually via Operations → Positions if "
                "you need to flatten."
            )
            if cols[1].button(
                "✅ Deactivate",
                type="primary",
                width="stretch",
                key="_cc_estop_off",
            ):
                account_manager.clear_emergency_stop()
                st.toast("Emergency stop CLEARED. Live trading resumed.")
                st.rerun()
        return

    # OFF state — green banner + clear "click to activate" button
    with st.container(border=True):
        cols = st.columns([3, 1, 1])
        cols[0].markdown(
            "### 🟢 Emergency stop — OFF\n"
            "Live trading is enabled. Click the red button to halt all "
            "order placement instantly. Optional checkboxes also close "
            "every open position and kill the runner process."
        )
        also_close = cols[0].checkbox(
            "🗑 Close every open position on the broker too (irreversible)",
            value=False, key="_cc_estop_close_all",
        )
        also_kill_runner = cols[0].checkbox(
            "🔪 Also kill the deployment-runner process",
            value=False, key="_cc_estop_kill_runner",
            help="If runner is a LaunchAgent it'll auto-restart after "
                  "10s. Otherwise the operator must `make run-live` "
                  "again manually.",
        )
        # One-click activation — no typed confirm. The two checkboxes
        # ARE the confirmation (intentional choices).
        cols[2].markdown("&nbsp;", unsafe_allow_html=True)
        if cols[2].button(
            "⛔ ACTIVATE NOW",
            type="primary",
            width="stretch",
            key="_cc_estop_on",
        ):
            account_manager.touch_emergency_stop()
            n_closed = 0
            if also_close:
                try:
                    from core.position_manager import PositionManager
                    from core.mt5_account import MT5AccountClient
                    pm = PositionManager(
                        account_login=login,
                        bridge=MT5AccountClient(),
                        db_path=DB_PATH,
                    )
                    results = pm.close_all(reason="emergency_flatten")
                    n_closed = sum(1 for r in results if r.ok)
                    st.toast(f"Closed {n_closed}/{len(results)} positions")
                except Exception as e:
                    st.error(f"close_all failed: {e}")
            if also_kill_runner:
                try:
                    subprocess.run(
                        ["pkill", "-f", "scripts/run_deployments.py"],
                        timeout=3, check=False,
                    )
                    st.toast("Runner process killed")
                except Exception:
                    pass
            # Also flip every live deployment to halted
            try:
                deps = dep_mod.load_deployments(login)
                changed = 0
                for d in deps:
                    if d.status == "live":
                        d.status = "halted"
                        d.notes = (
                            f"[EMERGENCY-STOP "
                            f"{datetime.now(timezone.utc).isoformat(timespec='minutes')}]"
                            + (f"\n{d.notes}" if d.notes else "")
                        )
                        changed += 1
                if changed:
                    dep_mod.save_deployments(login, deps)
                    st.toast(f"Halted {changed} live deployment(s)")
            except Exception as e:
                st.warning(f"deployments not flipped: {e}")
            st.rerun()


def _health_retest_one(d, *, parquet_root: Path) -> dict | None:
    """Re-run a single deployment's backtest using the SAME setup the
    catalog uses (rebaseline_catalog._run_one):
      - risk_pct=cost_defaults.DEFAULT_RISK_PCT + symbol_info for sizing
      - cost_defaults for commission / slippage / starting balance
      - resolve_money_per_unit per instrument
      - source_config_json from the catalog (so stop/target/lookback/etc.
        match the cell that was actually shipped)

    Pre-fix this panel called sweep_rr_winrate._backtest_one with
    fixed lots=0.1, hardcoded balance=91_400, and default stop/target
    1.5/3.0 — none of which match the catalog. That made deploy_safe
    cells (e.g. rsi_30_70 EURUSD M15 long, catalog PF 1.62) appear as
    PF 0.60 LOSING in the health panel. Same class of bug we already
    fixed in deployment_dialogs.py and Strategy Library deep-dive.
    """
    import dataclasses as _dc
    import json as _json

    from core import cost_defaults as _cd
    from core import edge_catalog as _ec
    from core.backtest import run_backtest
    from core.backtest_stats import compute_full_stats
    from core.data import load_parquet
    from core.symbol_info_loader import try_load as _try_load_si
    from dashboards.components.state import (
        discover_strategies as _discover,
        resolve_money_per_unit as _resolve_mpu,
    )
    from dashboards.components.strategy_resolver import resolve_base_strategy

    parquet = parquet_root / f"{d.ticker}_{d.tf}.parquet"
    if not parquet.exists():
        return {"error": f"no parquet for {d.ticker} {d.tf}"}

    strats = _discover()
    base = resolve_base_strategy(d.strategy, strats) or d.strategy
    if base not in strats:
        return {"error": f"strategy `{d.strategy}` (base `{base}`) "
                          "not in registry"}
    StratCls, ParamsCls = strats[base]

    # Pull source_config_json from the catalog so we use the EXACT
    # config that produced the catalog metric. Without this the run
    # uses strategy defaults — totally different trade counts.
    stored_cfg: dict = {}
    edge_row = _ec.best_for(d.ticker, d.tf, d.strategy)
    if edge_row is not None:
        cfg_json = getattr(edge_row, "source_config_json", "") or ""
        if cfg_json:
            try:
                stored_cfg = _json.loads(cfg_json)
            except (ValueError, TypeError):
                stored_cfg = {}

    # Build the strategy with the catalog config (or defaults if
    # nothing stored — markdown-only cells)
    if ParamsCls is not None:
        field_names = {f.name for f in _dc.fields(ParamsCls)}
        kwargs = {k: v for k, v in stored_cfg.items() if k in field_names}
        if "long_only" in field_names:
            kwargs["long_only"] = d.long_only
        try:
            strat = StratCls(ParamsCls(**kwargs))
        except TypeError:
            strat = StratCls()
    else:
        strat = StratCls()

    sym_info = _try_load_si(d.ticker)
    mpu = _resolve_mpu(d.ticker)
    try:
        df = load_parquet(parquet)
    except Exception as e:
        return {"error": f"load_parquet: {e}"}
    if len(df) < 200:
        return {"error": "parquet too short"}

    try:
        result = run_backtest(
            df, strat.signals(df),
            starting_balance=_cd.DEFAULT_STARTING_BALANCE_USD,
            lots=0.1,                # ignored when risk_pct is set
            money_per_unit_price=mpu,
            commission_per_trade=_cd.DEFAULT_COMMISSION_USD,
            slippage_per_fill_atr_frac=_cd.DEFAULT_SLIPPAGE_ATR_FRAC,
            symbol=d.ticker,
            risk_pct=(_cd.DEFAULT_RISK_PCT if sym_info is not None
                       else None),
            symbol_info=sym_info,
        )
        stats = compute_full_stats(
            result, starting_balance=_cd.DEFAULT_STARTING_BALANCE_USD,
        )
    except Exception as e:
        return {"error": f"backtest: {e}"}

    if not result.trades:
        return {"error": "no trades produced"}

    pf = stats.profit_factor if stats.profit_factor != float("inf") else 99.0
    avg_dollar = (result.sum_realized_pnl / max(1, result.n_trades))
    return {
        "n_trades": result.n_trades,
        "win_rate_pct": stats.win_rate_pct,
        "profit_factor": pf,
        "net_pnl_dollars": result.sum_realized_pnl,
        "dollar_per_trade": avg_dollar,
        "max_dd_pct": stats.max_dd_pct,
        "no_symbol_info": sym_info is None,
        "no_catalog_cfg": not bool(stored_cfg),
    }


def _render_cell_health_panel(login: int) -> None:
    """Re-test every live deployment with realistic costs and flag any
    that are losing money under real conditions.

    User's painful lesson: composer / strategy library show no-cost
    backtests. Once you add commission ($4/round-trip) + slippage
    (0.05×ATR) + per-instrument tick value, many M15/H1 cells flip
    from PF 1.5+ to PF <1. This panel surfaces those mismatches.
    """
    from core import deployment as dep_mod

    st.markdown("### 🪙  Cell health re-test  (cost-priced)")
    st.caption(
        "Re-runs each live deployment's backtest with the **same risk%-"
        "sizing + cost config the catalog uses** (so PF here matches "
        "the catalog row). Cells with PF < 1.0 are losing money under "
        "real conditions even though the no-cost backtest looks fine."
    )
    deps = dep_mod.load_deployments(login)
    cells = [d for d in deps if d.status in ("live", "paper")]
    if not cells:
        st.info("_No live or paper deployments to re-test._")
        return

    cols = st.columns([1, 1, 4])
    do_run = cols[0].button("🪙  Run health re-test", type="primary",
                              key="_cc_health_retest_btn")
    cols[1].caption(f"_{len(cells)} cell(s)_")
    cols[2].caption(
        "**Slippage** 0.05×ATR · **Commission** $4/round-trip · "
        "**Risk** 0.30%/trade · sizing uses symbol_info tick value"
    )

    if not do_run:
        return

    # Run the re-test
    parquet_root = REPO / "data"
    progress = st.progress(0.0, text="Re-testing deployments…")
    results: list[tuple] = []
    warned_no_si: list[str] = []
    warned_no_cfg: list[str] = []
    for i, d in enumerate(cells):
        try:
            row = _health_retest_one(d, parquet_root=parquet_root)
        except Exception as e:
            row = {"error": f"{type(e).__name__}: {e}"}
        if row and row.get("no_symbol_info"):
            warned_no_si.append(d.deployment_id)
        if row and row.get("no_catalog_cfg"):
            warned_no_cfg.append(d.deployment_id)
        results.append((d, row))
        progress.progress((i + 1) / len(cells),
                            text=f"Tested {i+1}/{len(cells)}: "
                                  f"{d.deployment_id}")
    progress.empty()

    if warned_no_si:
        st.warning(
            f"⚠ {len(warned_no_si)} cell(s) re-tested with fixed 0.1 "
            f"lots because symbol_info was missing — PF may diverge "
            f"from the catalog. Run `make refresh-symbol-info` to fix. "
            f"Affected: {', '.join(warned_no_si[:3])}"
            + ("…" if len(warned_no_si) > 3 else "")
        )
    if warned_no_cfg:
        st.info(
            f"ℹ {len(warned_no_cfg)} cell(s) re-tested with strategy "
            f"defaults (no source_config_json in the catalog — "
            f"markdown-only or pre-rebaseline rows). Re-run "
            f"`scripts/rebaseline_catalog.py` to populate. "
            f"Affected: {', '.join(warned_no_cfg[:3])}"
            + ("…" if len(warned_no_cfg) > 3 else "")
        )

    # Render the table + auto-archive UI for losers
    rows = []
    losers = []
    for d, r in results:
        if r is None or "error" in r:
            err = (r or {}).get("error", "unknown")
            rows.append({
                "Deployment": d.deployment_id,
                "Status": d.status,
                "WR%": "—", "PF": "—", "$/trade": "—",
                "n_trades": "—",
                "Verdict": f"⚠ {err[:40]}",
            })
            continue
        pf = r["profit_factor"]
        rows.append({
            "Deployment": d.deployment_id,
            "Status": d.status,
            "WR%": f"{r['win_rate_pct']:.1f}",
            "PF": f"{pf:.2f}",
            "$/trade": f"{r['dollar_per_trade']:.2f}",
            "n_trades": str(r["n_trades"]),
            "Verdict": ("🟢 healthy" if pf >= 1.10
                          else ("🟡 marginal" if pf >= 1.0
                                  else "🔴 LOSING")),
        })
        if pf < 1.0:
            losers.append((d, r))

    st.dataframe(pd.DataFrame(rows), width="stretch")

    if losers:
        st.warning(
            f"⚠️ {len(losers)} cell(s) below PF 1.0 with realistic "
            f"costs — losing money in production conditions.",
            icon="🔴",
        )
        st.markdown("##### What to do")
        for d, r in losers:
            cs = st.columns([4, 2, 2, 2])
            cs[0].markdown(
                f"**`{d.deployment_id}`**\n  "
                f"PF {r['profit_factor']:.2f} · "
                f"WR {r['win_rate_pct']:.1f}% · "
                f"{r['n_trades']} trades · "
                f"${r['dollar_per_trade']:.2f}/trade"
            )
            if cs[1].button("📝 Demote to paper",
                              key=f"_cc_health_demote_{d.deployment_id}"):
                d.status = "paper"
                d.notes = (
                    d.notes + " | demoted by Cell-health re-test "
                    f"(PF {r['profit_factor']:.2f} < 1.0)"
                )[-1500:]
                dep_mod.upsert_deployment(login, d)
                st.success(f"Demoted {d.deployment_id} to paper")
                time.sleep(0.5)
                st.rerun()
            if cs[2].button("🗑️  Archive",
                              key=f"_cc_health_archive_{d.deployment_id}"):
                d.status = "archived"
                d.notes = (
                    d.notes + " | archived by Cell-health re-test "
                    f"(PF {r['profit_factor']:.2f} < 1.0)"
                )[-1500:]
                dep_mod.upsert_deployment(login, d)
                st.success(f"Archived {d.deployment_id}")
                time.sleep(0.5)
                st.rerun()
            cs[3].caption("_keep:_ ignore (you may have a reason)")
    else:
        st.success("✅ All deployments are profitable under realistic costs.")


def _system_guardrails_editor(login: int) -> None:
    """Inline editor for risk_config.json + per-account circuit breaker.
    User: 'activate n deactivate emergency stop easy ui and system
    guardrail all these setting like now i setup 2% risk'."""
    from core import circuit_breaker as cb
    from core.config import load_config

    with st.expander("🛡️  System guardrails & risk caps  (click to edit)",
                        expanded=False):
        st.warning(
            "⚙️ **These settings are also editable on the [⚙️ Settings]"
            "(/Settings) page** — section B (Circuit breaker) + D "
            "(Per-deployment caps). Both pages write the same files, "
            "so values stay consistent. The Settings page is the "
            "canonical home; the editor here is being kept temporarily "
            "to avoid breaking workflows. Future cleanup will remove "
            "this duplicate.",
            icon="ℹ️",
        )
        st.info(
            "**Layer 2 of 2 — account-wide HALT/STOP_NEW.** When any "
            "threshold here is breached, the circuit breaker pauses "
            "EVERY live deployment (not just one). Saved to per-account "
            "`circuit_breaker.json`.\n\n"
            "Both layers fire independently — whichever trips first wins. "
            "Changes here take effect on the runner's next tick.",
            icon="🛡️",
        )

        # ── Account-level circuit breaker ────────────────────────────
        cfg = cb.load_config(login)
        st.markdown("##### 🔌 Per-account circuit breaker")
        rc1, rc2, rc3 = st.columns(3)
        new_daily_loss = rc1.number_input(
            "Daily loss limit ($)",
            value=float(cfg.daily_loss_dollars or 0),
            step=100.0, min_value=0.0,
            help="HALT all deployments when today's realized loss "
                  "exceeds this. Set 0 to disable.",
            key="_cc_cb_daily_loss",
        )
        new_total_loss = rc2.number_input(
            "Total loss limit ($)",
            value=float(cfg.total_loss_dollars or 0),
            step=100.0, min_value=0.0,
            help="HALT when lifetime realized loss exceeds this. "
                  "FTMO Challenge: usually 10% of starting balance.",
            key="_cc_cb_total_loss",
        )
        new_consec = rc3.number_input(
            "Max consecutive losses",
            value=int(cfg.max_consec_losses or 0),
            step=1, min_value=0, max_value=20,
            help="ACCOUNT-WIDE HALT after this many losing trades in a "
                  "row across all deployments. 0 = disabled. NOTE: "
                  "Account Risk's max_consecutive_losses is similar but "
                  "PER-STRATEGY (only that strategy pauses, others keep "
                  "going) — that one usually fires first.",
            key="_cc_cb_consec",
        )
        rc4, rc5, rc6 = st.columns(3)
        new_daily_target = rc4.number_input(
            "Daily target ($)",
            value=float(cfg.daily_target_dollars or 0),
            step=100.0, min_value=0.0,
            help="STOP_NEW (lock gains) when today's realized gain "
                  "exceeds this. 0 = disabled.",
            key="_cc_cb_daily_target",
        )
        new_total_target = rc5.number_input(
            "Total target ($)",
            value=float(cfg.total_target_dollars or 0),
            step=100.0, min_value=0.0,
            help="STOP_NEW when lifetime realized gain exceeds this. "
                  "FTMO Challenge: 8% of starting balance.",
            key="_cc_cb_total_target",
        )
        new_max_open = rc6.number_input(
            "Max open positions",
            value=int(cfg.max_open_positions or 0),
            step=1, min_value=0, max_value=50,
            help="ACCOUNT-WIDE STOP_NEW once this many positions are "
                  "open across all deployments. 0 = disabled. "
                  "DUPLICATED in Account Risk → max_open_positions — "
                  "smaller value wins. Keep both in sync to avoid "
                  "confusion.",
            key="_cc_cb_max_open",
        )
        new_halt_on_breach = st.checkbox(
            "Auto-halt all live deployments when HALT condition fires",
            value=cfg.halt_on_breach,
            key="_cc_cb_halt_on_breach",
        )
        if st.button("💾 Save circuit-breaker config", type="primary",
                       key="_cc_cb_save"):
            new_cfg = cb.CircuitConfig(
                daily_loss_dollars=new_daily_loss or None,
                total_loss_dollars=new_total_loss or None,
                daily_target_dollars=new_daily_target or None,
                total_target_dollars=new_total_target or None,
                max_consec_losses=int(new_consec) or None,
                max_open_positions=int(new_max_open) or None,
                halt_on_breach=new_halt_on_breach,
            )
            cb.save_config(login, new_cfg)
            st.success("✅ Circuit breaker saved.")
            time.sleep(0.5)
            st.rerun()

        st.markdown("---")
        st.markdown("##### ⚙️ Per-deployment risk · cap · max-lots  (quick edit)")
        st.caption(
            "**risk %** = position size on each trade · "
            "**daily cap %** = max loss per day before the deployment halts · "
            "**max lots** = hard ceiling on lot size (FTMO-style accounts "
            "usually 10–15; set 0 to disable). "
            "✏️  Edit any value — a `💾 Save` button appears at the bottom. "
            "Save persists to `deployments.json`; the runner picks up new "
            "values on its next tick (within 60 s) — no restart."
        )
        try:
            deps = dep_mod.load_deployments(login)
        except Exception as e:
            st.error(f"could not load deployments: {e}")
            return
        running = [d for d in deps if d.status in ("paper", "live")]
        if not running:
            st.info("_no live/paper deployments to edit._")
            return
        # Header row
        hdr = st.columns([4, 2, 2, 2, 2, 2])
        hdr[0].markdown("**Deployment**")
        hdr[1].markdown("<span style='color:#9ca3af;font-size:0.75rem;'>"
                          "risk %</span>", unsafe_allow_html=True)
        hdr[2].markdown("<span style='color:#9ca3af;font-size:0.75rem;'>"
                          "daily cap %</span>", unsafe_allow_html=True)
        hdr[3].markdown("<span style='color:#9ca3af;font-size:0.75rem;'>"
                          "max lots</span>", unsafe_allow_html=True)
        hdr[4].markdown("<span style='color:#9ca3af;font-size:0.75rem;'>"
                          "max $/trade</span>", unsafe_allow_html=True)
        hdr[5].markdown("<span style='color:#9ca3af;font-size:0.75rem;'>"
                          "status</span>", unsafe_allow_html=True)

        edits = []
        for d in running:
            row = st.columns([4, 2, 2, 2, 2, 2])
            row[0].markdown(
                f"**`{d.strategy}`** · `{d.ticker}` `{d.tf}`"
            )
            new_risk = float(row[1].number_input(
                "risk %",
                value=float(d.risk_pct),
                min_value=0.05, max_value=5.0, step=0.05,
                format="%.2f",
                key=f"_cc_risk_{d.deployment_id}",
                label_visibility="collapsed",
            ))
            new_cap = float(row[2].number_input(
                "daily cap %",
                value=float(d.daily_cap_pct),
                min_value=0.1, max_value=10.0, step=0.1,
                format="%.2f",
                key=f"_cc_cap_{d.deployment_id}",
                label_visibility="collapsed",
            ))
            current_max = float(getattr(d, "max_lots", 15.0) or 0.0)
            new_max = float(row[3].number_input(
                "max lots",
                value=current_max,
                min_value=0.0, max_value=1000.0, step=0.5,
                format="%.2f",
                key=f"_cc_maxlots_{d.deployment_id}",
                label_visibility="collapsed",
                help="Hard cap on lots per trade (0 = no cap, only "
                       "broker volume_max applies). FTMO-style accounts "
                       "typically allow 10–15 lots on indices.",
            ))
            current_max_usd = float(getattr(d, "max_money_risk_usd", 0.0)
                                       or 0.0)
            new_max_usd = float(row[4].number_input(
                "max $/trade",
                value=current_max_usd,
                min_value=0.0, max_value=5000.0, step=10.0,
                format="%.0f",
                key=f"_cc_maxusd_{d.deployment_id}",
                label_visibility="collapsed",
                help="HARD $-ceiling per trade. Even if risk_pct says you "
                       "can lose more, this caps you. 0 = no cap. "
                       "Composer auto-fills this at deploy time as "
                       "(equity × daily_cap_pct/100) ÷ trades_per_day. "
                       "Crucial against tight-stop blow-ups on gold/oil.",
            ))
            row[5].markdown(
                "🟢 LIVE" if d.status == "live" else "🟡 PAPER"
            )
            if ((new_risk != d.risk_pct) or (new_cap != d.daily_cap_pct)
                or (new_max != current_max)
                or (new_max_usd != current_max_usd)):
                edits.append((d, new_risk, new_cap, new_max, new_max_usd))

        if edits:
            if st.button(
                f"💾 Save {len(edits)} change(s)", type="primary",
                key="_cc_dep_save",
            ):
                for d, new_risk, new_cap, new_max, new_max_usd in edits:
                    d.risk_pct = new_risk
                    d.daily_cap_pct = new_cap
                    d.max_lots = new_max
                    d.max_money_risk_usd = new_max_usd
                    dep_mod.upsert_deployment(login, d)
                st.success(f"✅ Saved {len(edits)} deployment(s)")
                time.sleep(0.5)
                st.rerun()


# ─────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="Command Center", page_icon="⚡",
                          layout="wide")
    theme.inject_css()
    st.title("⚡  Command Center")
    st.caption(
        "Single-page system status. Refresh the browser to update — every "
        "tile probes the live system, no caching."
    )

    accounts = account_manager.list_accounts()
    if not accounts:
        st.warning("No MT5 account configured. Add one on the Operations "
                    "page → Settings tab.")
        return
    active = accounts[0]

    deployments = dep_mod.load_deployments(active.login)
    live_deps = [d for d in deployments if d.status == "live"]
    paper_deps = [d for d in deployments if d.status == "paper"]
    halted_deps = [d for d in deployments if d.status == "halted"]

    # ─── Section 1: Emergency stop ────────────────────────────────────
    _emergency_stop_panel(active.login)

    # ─── Section 1a: Today's risk budget (adaptive_risk live tile) ───
    _render_daily_risk_budget_tile(active.login)

    # ─── Section 1a-bis: Edge-decay autolearner ──────────────────────
    _render_edge_decay_tile(active.login, deployments)

    # ─── Section 1b: System guardrails (collapsed editor) ────────────
    _system_guardrails_editor(active.login)

    st.markdown("---")

    # ─── Section 2: Header strip — 6 tiles ───────────────────────────
    st.markdown("### 🩺  System health")
    bridge_state, bridge_detail, bridge_info = _bridge_status()
    n_open_positions = 0
    if bridge_state == "ok":
        try:
            from core.mt5_account import MT5AccountClient
            n_open_positions = len(MT5AccountClient().positions_get())
        except Exception:
            pass

    runner_state, runner_detail = _runner_heartbeat(active.login)
    cb_state, cb_detail, cb_reasons = _circuit_breaker_status(
        active.login, n_open_positions,
    )

    tiles = st.columns(6)
    _tile("Runner heartbeat", runner_state, runner_detail, tiles[0])
    _tile("MT5 bridge", bridge_state, bridge_detail, tiles[1])
    _tile("Open positions", "ok" if n_open_positions <= 6 else "warn",
            f"{n_open_positions} on broker", tiles[2])
    if bridge_info:
        equity_change_pct = (
            (bridge_info["equity"] - active.effective_baseline_equity)
            / active.effective_baseline_equity * 100.0
        )
        eq_state = ("ok" if equity_change_pct > -3
                    else "warn" if equity_change_pct > -8
                    else "halt")
        _tile("Account equity",
                eq_state,
                f"${bridge_info['equity']:,.0f} "
                f"({equity_change_pct:+.2f}% vs baseline)",
                tiles[3])
    else:
        _tile("Account equity", "down", "bridge offline", tiles[3])
    _tile("Circuit breaker", cb_state, cb_detail, tiles[4])
    _tile("Deployments",
            "ok" if live_deps or paper_deps else "warn",
            f"🟢 {len(live_deps)} live · 🟡 {len(paper_deps)} paper · "
            f"⛔ {len(halted_deps)} halted",
            tiles[5])

    if cb_reasons:
        st.error("Circuit breaker reasons:\n" +
                  "\n".join(f"- {r}" for r in cb_reasons))

    # ─── Section 3: Component health grid ────────────────────────────
    st.markdown("---")
    st.markdown("### 🧩  Component health")
    grid = st.columns(4)

    mt5_state, mt5_detail = _mt5_process_status()
    _tile("MetaTrader 5 process", mt5_state, mt5_detail, grid[0])

    nap_state, nap_detail = _app_nap_disabled()
    _tile("App Nap", nap_state, nap_detail, grid[1])

    runner_la_state, runner_la_detail = _launchagent_status(
        RUNNER_PLIST, "com.user.mt5_runner",
    )
    # Context-aware softening: a missing plist is NOT an emergency if
    # the runner_state.json heartbeat is fresh — that means the user
    # is running `make run-live` manually instead of via the LaunchAgent
    # (a perfectly valid setup, recommended for active development).
    # Only flag red if BOTH the plist is missing AND the runner is dead.
    if (runner_la_state == "missing"
            and runner_state == "ok"):
        runner_la_state = "ok"
        runner_la_detail = (
            "not installed · using manual `make run-live` "
            "(heartbeat fresh — fine)"
        )
    _tile("Runner LaunchAgent", runner_la_state, runner_la_detail, grid[2])

    keep_la_state, keep_la_detail = _launchagent_status(
        KEEP_MT5_PLIST, "com.user.keep_mt5_alive",
    )
    _tile("Keep-MT5 LaunchAgent", keep_la_state, keep_la_detail, grid[3])

    grid2 = st.columns(4)
    sym_state, sym_detail, sym_missing = _symbol_info_coverage(deployments)
    _tile("symbol_info coverage", sym_state, sym_detail, grid2[0])

    par_state, par_detail, par_stale = _parity_coverage(deployments)
    _tile("Parity coverage", par_state, par_detail, grid2[1])

    # Quality gate violations (live deployments only)
    n_quality_block = 0
    try:
        from core import edge_catalog
        from core.deployment_quality_gate import (
            evaluate_quality, load_criteria,
        )
        criteria = load_criteria(active.login)
        for d in live_deps:
            try:
                es = edge_catalog.best_for(d.ticker, d.tf, d.strategy)
                if es is not None:
                    if evaluate_quality(es, criteria=criteria).is_blocked:
                        n_quality_block += 1
            except Exception:
                continue
    except Exception:
        pass
    _tile("Quality gate",
            "ok" if n_quality_block == 0 else "warn",
            f"{n_quality_block} live deps blocked"
              if n_quality_block else "all live deps OK",
            grid2[2])

    # Position-guard collisions
    n_collisions = 0
    if bridge_state == "ok":
        try:
            from core.mt5_account import MT5AccountClient
            from core.position_guard import (
                OpenPosition, collisions_in_open_set,
            )
            positions = MT5AccountClient().positions_get()
            sym_to_dep = {}
            for d in deployments:
                sym_to_dep.setdefault(d.ticker, d.deployment_id)
            ops = []
            for p in positions:
                ops.append(OpenPosition(
                    deployment_id=sym_to_dep.get(p.symbol,
                                                  f"unknown:{p.symbol}"),
                    symbol=p.symbol,
                    side="LONG" if int(p.type) == 0 else "SHORT",
                    lots=float(p.volume),
                    opened_at_utc="",
                ))
            n_collisions = len(collisions_in_open_set(ops))
        except Exception:
            pass
    _tile("Position collisions",
            "ok" if n_collisions == 0 else "warn",
            f"{n_collisions} pair(s)" if n_collisions else "none",
            grid2[3])

    # ─── Drill-downs for warnings ────────────────────────────────────
    # Surface keep_mt5_alive failures inline if the LaunchAgent is unhappy
    if keep_la_state == "warn":
        err_path = Path("/tmp/mt5_alive.err")
        log_path = Path("/tmp/mt5_alive.log")
        with st.expander(
            "⚠ Keep-MT5 LaunchAgent — last error details", expanded=False,
        ):
            st.caption(
                "The keep-MT5-alive LaunchAgent is loaded but not currently "
                "running. Common causes: MT5 wasn't installed at the path "
                "the script expects, or the script crashed during initial "
                "launch. Recent stderr/stdout below."
            )
            if err_path.exists():
                try:
                    txt = err_path.read_text(errors="replace").splitlines()[-30:]
                    st.code("\n".join(txt) if txt
                              else "(empty)", language="text")
                except Exception as e:
                    st.error(f"could not read {err_path}: {e}")
            else:
                st.info(f"_{err_path} not present yet._")
            if log_path.exists():
                try:
                    txt = log_path.read_text(errors="replace").splitlines()[-15:]
                    st.caption("Tail of `/tmp/mt5_alive.log`:")
                    st.code("\n".join(txt) if txt
                              else "(empty)", language="text")
                except Exception:
                    pass
            cols = st.columns(2)
            if cols[0].button("🔄 Reload keep-MT5 LaunchAgent",
                                 key="_cc_reload_keep_mt5"):
                try:
                    subprocess.run(
                        ["launchctl", "unload", str(KEEP_MT5_PLIST)],
                        timeout=5, check=False,
                    )
                    subprocess.run(
                        ["launchctl", "load", str(KEEP_MT5_PLIST)],
                        timeout=5, check=False,
                    )
                    st.success("✅ reloaded — re-check the tile")
                    time.sleep(0.5)
                    st.rerun()
                except Exception as e:
                    st.error(f"⛔ {e}")
            cols[1].caption(
                "Or run `make keep-mt5-alive` in a terminal for a "
                "foreground keep-alive loop."
            )

    if sym_missing:
        with st.expander(
            f"⚠ Missing / invalid symbol_info for {len(sym_missing)} ticker(s)"
        ):
            st.write(", ".join(f"`{t}`" for t in sym_missing))
            st.markdown(
                "**Most common cause:** the bridge EA returned "
                "`tick_value=0`. That happens on the older `MT5BridgeFile` "
                "schema — recompile the EA in MetaEditor (we already accept "
                "both old + new field names in the python client, but if the "
                "JSON itself is missing the value there's nothing to read)."
            )
            st.code("make refresh-symbol-info", language="bash")
            st.caption(
                "Click the **🔄 Refresh symbol_info** button below to "
                "re-pull from MT5 and confirm the fix."
            )
    if par_stale:
        with st.expander(
            f"⚠ Stale parity for {len(par_stale)} base strateg"
            f"{'y' if len(par_stale) == 1 else 'ies'}"
        ):
            st.write(", ".join(f"`{b}`" for b in par_stale))
            st.caption("Open the 🔬 Replay-Parity page to refresh.")

    # ─── Section 3.5: Cell health re-test (cost-priced) ──────────────
    st.markdown("---")
    _render_cell_health_panel(active.login)

    # ─── Section 4: Quick actions ────────────────────────────────────
    st.markdown("---")
    st.markdown("### ⚡  Quick actions")
    qa = st.columns(4)
    if qa[0].button("🔄 Refresh symbol_info",
                       width="stretch",
                       key="_cc_refresh_sym"):
        try:
            res = subprocess.run(
                [sys.executable, str(REPO / "scripts/refresh_symbol_info.py")],
                capture_output=True, text=True, timeout=30,
            )
            if res.returncode == 0:
                st.success("✅ symbol_info refreshed")
            else:
                st.error(f"⛔ refresh failed: {res.stderr[:300]}")
        except Exception as e:
            st.error(f"⛔ {e}")
    if qa[1].button("🔍 Run preflight check",
                       width="stretch",
                       key="_cc_preflight"):
        try:
            res = subprocess.run(
                [sys.executable, str(REPO / "scripts/preflight_check.py")],
                capture_output=True, text=True, timeout=60,
            )
            with st.expander("Preflight output", expanded=True):
                st.code(res.stdout + ("\nSTDERR:\n" + res.stderr
                                          if res.stderr else ""))
        except Exception as e:
            st.error(f"⛔ {e}")
    if qa[2].button("🔁 Restart runner",
                       width="stretch",
                       key="_cc_restart_runner",
                       help="If runner is a LaunchAgent, this unloads + "
                             "reloads. Otherwise sends SIGTERM and the "
                             "operator must `make run-live` manually."):
        try:
            if RUNNER_PLIST.exists():
                subprocess.run(
                    ["launchctl", "unload", str(RUNNER_PLIST)],
                    timeout=5, check=False,
                )
                subprocess.run(
                    ["launchctl", "load", str(RUNNER_PLIST)],
                    timeout=5, check=False,
                )
                st.success("✅ LaunchAgent reloaded")
            else:
                subprocess.run(
                    ["pkill", "-f", "scripts/run_deployments.py"],
                    timeout=3, check=False,
                )
                st.warning(
                    "Runner killed. LaunchAgent isn't installed — "
                    "run `make run-live` to restart."
                )
        except Exception as e:
            st.error(f"⛔ {e}")
    if qa[3].button("🩺 Run health check",
                       width="stretch",
                       key="_cc_health"):
        try:
            res = subprocess.run(
                [sys.executable, str(REPO / "scripts/runner_health.py")],
                capture_output=True, text=True, timeout=20,
            )
            with st.expander("Health-check output", expanded=True):
                st.code(res.stdout)
        except Exception as e:
            st.error(f"⛔ {e}")

    # ─── Section 5: Live activity feed ────────────────────────────────
    st.markdown("---")
    st.markdown("### 📜  Recent bridge activity")
    try:
        import sqlite3
        with sqlite3.connect(str(account_manager.get_db_path(active.login))) as c:
            df = pd.read_sql_query(
                """SELECT pinged_at_utc, method, ok, latency_ms, error
                   FROM bridge_events
                   ORDER BY ROWID DESC LIMIT 50""",
                c,
            )
        if df.empty:
            st.info("_No bridge events recorded yet._")
        else:
            df["status"] = df["ok"].apply(
                lambda v: "✅" if v else "⛔")
            df = df[["pinged_at_utc", "status", "method", "latency_ms", "error"]]
            df.columns = ["Time UTC", "OK", "Method", "Latency ms", "Error"]
            st.dataframe(df, width="stretch", height=320)
    except Exception as e:
        st.warning(f"could not load bridge_events: {e}")

    # ─── Section 6: Runner log tail ──────────────────────────────────
    st.markdown("---")
    st.markdown("### 📝  Runner log (last 30 lines)")
    if RUNNER_LOG.exists():
        try:
            lines = RUNNER_LOG.read_text(errors="replace").splitlines()
            tail = lines[-30:]
            st.code("\n".join(tail) if tail
                       else "(log empty)", language="text")
            st.caption(f"Source: {RUNNER_LOG}")
        except Exception as e:
            st.warning(f"could not read log: {e}")
    else:
        st.info(
            f"_{RUNNER_LOG} not found. Runner runs in foreground or "
            f"the LaunchAgent isn't installed yet — use_ "
            f"`make install-runner-launchagent` _to enable persistent "
            f"logs._"
        )

    if RUNNER_ERR.exists() and RUNNER_ERR.stat().st_size > 0:
        with st.expander(f"⚠ Runner error log ({RUNNER_ERR})", expanded=False):
            try:
                err = RUNNER_ERR.read_text(errors="replace").splitlines()
                st.code("\n".join(err[-50:]), language="text")
            except Exception:
                st.warning("could not read error log")


main()
