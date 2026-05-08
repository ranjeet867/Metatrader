#!/usr/bin/env python3
"""
runner_health.py — confirm the deployment runner is alive and healthy.

Run anytime you come back from vacation / get nervous about the
unattended runner / want to verify the LaunchAgent is doing its job.

Checks (each prints ✅ / 🟡 / ⛔):
  • runner_state.json exists and has a recent saved_at_utc heartbeat
    (within 3× poll_seconds — anything older means the runner is hung
    or crashed)
  • bridge is reachable (live broker equity > 0)
  • last live trade was within the expected fire frequency (most
    deployments fire ≥1 trade per week; if no trades in 7+ days for
    every deployment, something's wrong)
  • LaunchAgent (if installed) is loaded
  • bridge_events table shows recent successful order_send + position_close
    events (no >12h silence on healthy markets)

Exit code:
  0 — all healthy
  1 — at least one ⛔
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _ensure_venv() -> None:
    try:
        import pandas  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    import os
    venv_py = REPO / ".venv" / "bin" / "python"
    if not venv_py.exists():
        venv_py = REPO / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists() and os.environ.get("_RH_RELAUNCHED") != "1":
        os.environ["_RH_RELAUNCHED"] = "1"
        os.execv(str(venv_py), [str(venv_py), __file__, *sys.argv[1:]])
    print("⛔ pandas missing — run `make setup` first.")
    sys.exit(2)


_ensure_venv()


def _ok(msg): print(f"  ✅ {msg}")
def _warn(msg): print(f"  🟡 {msg}")
def _err(msg): print(f"  ⛔ {msg}")
def _info(msg): print(f"     {msg}")


def check_runner_state(login: int, max_stale_minutes: float) -> int:
    """Returns 1 if state file is missing / stale, 0 if fresh."""
    from core import account_manager
    db_path = account_manager.get_db_path(login)
    state_file = db_path.parent / "runner_state.json"
    if not state_file.exists():
        _err(f"runner_state.json not found at {state_file}")
        _info("→ The runner has never written state. Did you run "
              "`make run-live` since last reboot?")
        return 1
    import json
    data = json.loads(state_file.read_text())
    saved_at = data.get("saved_at_utc")
    if saved_at is None:
        _err(f"runner_state.json has no saved_at_utc")
        return 1
    saved_dt = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - saved_dt
    age_min = age.total_seconds() / 60.0
    if age_min > max_stale_minutes:
        _err(f"runner heartbeat STALE — last save {age_min:.1f} minutes ago "
             f"(limit {max_stale_minutes:.0f}). Runner is hung or crashed.")
        _info("→ Check `tail -100 /tmp/mt5_runner.log` for the last "
              "tick output. Restart with `make run-live` or "
              "`launchctl unload + load` if installed as LaunchAgent.")
        return 1
    n_deps = len(data.get("last_seen_bar", {}))
    _ok(f"runner heartbeat fresh — last save {age_min:.1f} min ago, "
        f"tracking {n_deps} deployment(s)")
    return 0


def check_launchagent_status() -> int:
    """Returns 1 if installed but not running, 0 otherwise."""
    plist = Path.home() / "Library/LaunchAgents/com.user.mt5_runner.plist"
    if not plist.exists():
        _info("LaunchAgent NOT installed "
              "(use `make install-runner-launchagent` for vacation mode)")
        return 0
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except Exception:
        _warn("could not query launchctl")
        return 0
    if "com.user.mt5_runner" in result.stdout:
        for line in result.stdout.splitlines():
            if "com.user.mt5_runner" in line:
                pid = line.split("\t")[0]
                if pid == "-":
                    _err(f"LaunchAgent loaded but NOT RUNNING — last exit "
                         f"abnormal. Check /tmp/mt5_runner.err.")
                    return 1
                _ok(f"LaunchAgent running · pid={pid}")
                return 0
    _warn("LaunchAgent plist present but not in launchctl list — "
          "try `launchctl load ~/Library/LaunchAgents/com.user.mt5_runner.plist`")
    return 0


def check_bridge(login: int) -> int:
    """Live bridge round-trip + equity > 0."""
    try:
        from core.mt5_account import MT5AccountClient
        client = MT5AccountClient()
        info = client.account_info(force_refresh=True)
        if info.equity <= 0:
            _err(f"broker equity is 0 — account dead?")
            return 1
        _ok(f"bridge OK · balance=${info.balance:,.2f} "
            f"equity=${info.equity:,.2f}")
        return 0
    except Exception as e:
        _err(f"bridge UNREACHABLE: {type(e).__name__}: {e}")
        _info("→ MetaTrader 5 not running, EA not attached, or bridge "
              "files locked. Run `make keep-mt5-alive-check`.")
        return 1


def check_recent_activity(login: int, max_silent_hours: float) -> int:
    """Look at bridge_events + recent live trade closes for activity.

    Primary alive signal is runner_state.json freshness (checked
    separately). This check is a secondary "did the runner actually
    interact with the bridge?" signal.

    Pre-fix: this was the ONLY pass/fail check on activity, so a quiet
    day (no trades fired AND no heartbeat bridge_event row) would
    generate a false ⛔ even though the runner was healthy. Post-fix:
    the runner now emits a method='runner_heartbeat' bridge row every
    minute, so absence of ANY row in 12h IS a real alarm.

    A quiet-but-alive runner now produces ≥720 heartbeat rows per 12h.
    """
    from core import account_manager
    db_path = account_manager.get_db_path(login)
    if not db_path.exists():
        _warn(f"v2.db not found at {db_path}")
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_silent_hours)
    cutoff_iso = cutoff.isoformat()
    try:
        with sqlite3.connect(str(db_path)) as c:
            row = c.execute(
                "SELECT COUNT(*) FROM bridge_events "
                "WHERE substr(pinged_at_utc, 1, 19) >= ?",
                (cutoff_iso[:19],),
            ).fetchone()
            n_events = row[0] if row else 0
            # Heartbeat-only rows are the new floor. Trade-related rows
            # are the meaningful signal we want to highlight.
            row = c.execute(
                "SELECT COUNT(*) FROM bridge_events "
                "WHERE substr(pinged_at_utc, 1, 19) >= ? "
                "  AND method != 'runner_heartbeat'",
                (cutoff_iso[:19],),
            ).fetchone()
            n_trade_events = row[0] if row else 0
            row = c.execute(
                "SELECT COUNT(*) FROM trades WHERE mode='live' "
                "AND closed_at_utc IS NOT NULL "
                "AND substr(closed_at_utc, 1, 19) >= ?",
                (cutoff_iso[:19],),
            ).fetchone()
            n_recent_closes = row[0] if row else 0
    except sqlite3.OperationalError as e:
        _warn(f"could not query journal: {e}")
        return 0
    if n_events == 0:
        # If runner_state.json was fresh (validated by the heartbeat
        # check earlier), this is a runner-version mismatch — runner
        # is alive but pre-dates the runner_heartbeat patch. Surface
        # as a 🟡 warning, not a ⛔ failure.
        from core import account_manager as _am
        state_file = _am.get_db_path(login).parent / "runner_state.json"
        if state_file.exists():
            try:
                import json
                saved = json.loads(state_file.read_text()).get("saved_at_utc")
                saved_dt = datetime.fromisoformat(
                    saved.replace("Z", "+00:00"))
                age_min = (datetime.now(timezone.utc) - saved_dt).total_seconds() / 60
                if age_min < 5:
                    _warn(f"NO bridge events in last "
                          f"{max_silent_hours:.0f}h, BUT runner_state is "
                          f"fresh ({age_min:.1f} min old). Runner is "
                          f"alive — likely pre-dates the runner_heartbeat "
                          f"patch. Restart the runner to populate.")
                    return 0
            except Exception:
                pass
        _err(f"NO bridge events in last {max_silent_hours:.0f}h. "
             f"Runner may be tick-deduping every signal "
             f"(no new bars from broker?), or runner is stopped.")
        _info("→ Verify with: `tail -100 /tmp/mt5_runner.log`. "
              "Restart with `make run-live` if dead.")
        return 1
    _ok(f"{n_events} bridge events in last {max_silent_hours:.0f}h "
        f"({n_trade_events} trade-related, {n_recent_closes} live closes)")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Runner health check.")
    p.add_argument("--login", type=int, default=None,
                    help="Specific account. Default: first.")
    p.add_argument("--max-stale-minutes", type=float, default=5.0,
                    help="Max acceptable heartbeat age (default 5min).")
    p.add_argument("--max-silent-hours", type=float, default=12.0,
                    help="Max acceptable bridge-events silence "
                          "(default 12h — handles weekend market closures).")
    args = p.parse_args()

    print("🔍  Runner health check\n")
    from core import account_manager
    accounts = account_manager.list_accounts()
    if not accounts:
        print("⛔ no accounts configured")
        return 1
    if args.login:
        active = next((a for a in accounts if a.login == args.login), None)
    else:
        active = accounts[0]
    if active is None:
        print(f"⛔ no account with login={args.login}")
        return 1
    print(f"Account: {active.alias} (#{active.login})\n")

    n_err = 0
    print("• Bridge connectivity")
    n_err += check_bridge(active.login)
    print("\n• Runner heartbeat")
    n_err += check_runner_state(active.login, args.max_stale_minutes)
    print("\n• LaunchAgent")
    n_err += check_launchagent_status()
    print("\n• Recent activity (trades + bridge events)")
    n_err += check_recent_activity(active.login, args.max_silent_hours)

    print()
    if n_err == 0:
        print("✅  Runner healthy.")
        return 0
    print(f"⛔  {n_err} health check(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
