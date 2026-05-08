#!/usr/bin/env python3
"""
run_deployments.py — start the live/paper deployment runner.

This is the daemon that ACTUALLY drives your deployments. Without
running it, deployments marked 'paper' or 'live' in the UI are just
JSON labels — no bars are polled, no orders are sent.

USAGE:
  python scripts/run_deployments.py                  # use first account
  python scripts/run_deployments.py --login 12345    # specific account
  python scripts/run_deployments.py --poll 30        # tick every 30s
  python scripts/run_deployments.py --once           # one tick, then exit
                                                       (good for cron)

The runner:
  1. Reads deployments.json fresh on every tick — UI changes apply
     to the next loop iteration (no restart required).
  2. Fetches the latest bars from the MT5 bridge per (ticker, tf).
  3. For each deployment in 'paper' or 'live' status, calls
     core.runner.tick(...) which can:
        - Close existing positions (SL/TP/forced-flat)
        - Open new positions on signal-fire
  4. Live opens are mirrored to the broker via LiveExecutor.send_order.
  5. Circuit breaker + position guard + parity gate + quality gate
     are all consulted on every tick.

Stop with Ctrl+C — the runner shuts down cleanly.

Logs go to STDOUT and to the SQLite bridge_events table.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _ensure_venv() -> None:
    """Re-launch under .venv/bin/python if pandas is missing."""
    try:
        import pandas  # noqa: F401
        return
    except ModuleNotFoundError:
        pass
    import os
    venv_py = REPO / ".venv" / "bin" / "python"
    if not venv_py.exists():
        venv_py = REPO / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists() and os.environ.get("_RD_RELAUNCHED") != "1":
        os.environ["_RD_RELAUNCHED"] = "1"
        os.execv(str(venv_py), [str(venv_py), __file__, *sys.argv[1:]])
    print("⛔ pandas missing — run `make setup` first or activate .venv.")
    sys.exit(2)


_ensure_venv()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Start the live/paper deployment runner.",
    )
    p.add_argument("--login", type=int, default=None,
                    help="Account login. Default: first account in registry.")
    p.add_argument("--poll", type=float, default=5.0,
                    help="Seconds between ticks. Default 5 (was 60). "
                          "5s on M15 = max 5s lag between bar-close and "
                          "order-send (vs 60s previously). Bar-close-aware "
                          "scheduling kicks in automatically when a "
                          "fresh bar is detected, so most ticks fire "
                          "within 1-2s of bar close. Override only if "
                          "you have a reason — bridge handles 720 polls/h "
                          "trivially since most are no-op 'no_new_bar' "
                          "passes that don't compute signals.")
    p.add_argument("--once", action="store_true",
                    help="Run a single tick and exit (useful for cron).")
    p.add_argument("--dry-run", action="store_true",
                    help="Read deployments + bars but don't actually send "
                          "orders to the broker. Paper executors still update.")
    p.add_argument("--max-history", type=int, default=500,
                    help="Bars to fetch per tick (default 500).")
    p.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--rebaseline-interval-days", type=int, default=7,
                    help="Run weekly catalog rebaseline as a background "
                          "thread. Default 7 days. Set to 0 to disable.")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    log = logging.getLogger("run_deployments")

    # ----- Resolve account + bridge -----
    from core import account_manager
    from core.deployment_runner import DeploymentRunner
    from core.mt5_account import MT5AccountClient

    accounts = account_manager.list_accounts()
    if not accounts:
        log.error("⛔ no accounts configured — add one on Operations page.")
        return 1
    if args.login is None:
        active = accounts[0]
        log.info("Using first account: %s (#%s)", active.alias, active.login)
    else:
        active = next((a for a in accounts if a.login == args.login), None)
        if active is None:
            log.error("⛔ no account with login=%s in registry", args.login)
            return 1

    bridge = MT5AccountClient()
    bridge_call = None if args.dry_run else bridge._call

    # ----- Candle fetcher: pulls latest bars from the MT5 bridge -----
    # CRITICAL: the EA's copy_rates handler reads:
    #   params.name        — the symbol  (NOT 'symbol')
    #   params.timeframe   — the tf      (NOT 'tf')
    #   params.count       — number of bars
    # The MT5BridgeFile.mq5 dispatcher at line 67-71 documents this. Sending
    # 'symbol'/'tf' silently returns [] (no error, no bars).
    def _candle_fetcher(symbol: str, tf: str, n: int):
        import pandas as pd
        # Phase-32 #240: retry-once on transient bridge timeout. The
        # bridge file-queue is serialized through the EA single-thread,
        # so when Data Manager fires N parallel fetches the runner's
        # tick can wait past the 10s default timeout. A single retry
        # with 1s backoff catches >95% of transient saturation cases
        # without delaying real failures by more than ~1s.
        import time as _time
        last_err = None
        for attempt in range(2):
            try:
                resp = bridge._call("copy_rates",
                                      {"name": symbol,
                                       "timeframe": tf,
                                       "count": int(n)})
                break   # success
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                # Retry only on timeouts — other errors (bad symbol,
                # bridge disconnect) will fail the same way again.
                if attempt == 0 and ("timeout" in msg or "did not respond" in msg):
                    log.info("copy_rates(%s, %s) transient timeout, retrying once...",
                             symbol, tf)
                    _time.sleep(1.0)
                    continue
                log.warning("copy_rates(%s, %s) failed: %s", symbol, tf, e)
                return pd.DataFrame()
        else:
            log.warning("copy_rates(%s, %s) failed after retry: %s",
                        symbol, tf, last_err)
            return pd.DataFrame()
        # Bridge wraps the result as {"ok": True, "data": [...]} or returns
        # the list directly. Accept both shapes.
        if isinstance(resp, list):
            rows = resp
        elif isinstance(resp, dict):
            rows = resp.get("data", resp.get("rates", []))
            if isinstance(rows, str):
                # Some bridge builds JSON-encode the inner array as a string
                import json
                try:
                    rows = json.loads(rows)
                except json.JSONDecodeError:
                    rows = []
        else:
            rows = []
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        if "time" not in df.columns:
            log.warning("copy_rates returned rows without 'time' column "
                          "(symbol=%s tf=%s, columns=%s)",
                          symbol, tf, list(df.columns))
            return pd.DataFrame()
        # EA emits time as Unix epoch seconds (MqlRates.time is datetime
        # but kv_int stringifies it as long). Convert.
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True,
                                       errors="coerce")
        df = df.dropna(subset=["time"]).sort_values("time")
        return df.reset_index(drop=True)

    runner = DeploymentRunner(
        login=active.login,
        candle_fetcher=_candle_fetcher,
        bridge_call=bridge_call,
        poll_seconds=args.poll,
        max_history_bars=args.max_history,
    )

    log.info("DeploymentRunner ready. Account=%s, poll=%ss, dry_run=%s",
             active.login, args.poll, args.dry_run)

    if args.once:
        records = runner.tick_once()
        log.info("Single tick complete — %d deployment record(s)",
                 len(records))
        for r in records:
            reason = getattr(r, "skip_reason", "") or "—"
            log.info("  %s: opens=%d closes=%d skipped=%d (%s)  err=%r",
                     r.deployment_id, r.n_opens, r.n_closes,
                     r.n_skipped, reason, r.error)
        return 0

    # ----- Long-running mode -----
    log.info("Running first tick immediately (so you know it works)...")
    first_records = runner.tick_once()
    if not first_records:
        log.warning("First tick produced 0 deployment records — check that "
                     "you have deployments in 'paper' or 'live' status. "
                     "Use the dashboard's Operations page to see them.")
    else:
        log.info("First tick complete — %d deployment(s) processed.",
                 len(first_records))
        for r in first_records:
            level = logging.WARNING if r.error else logging.INFO
            reason = getattr(r, "skip_reason", "") or "—"
            log.log(level,
                     "  %-50s opens=%d closes=%d skipped=%d (%s)  err=%r",
                     r.deployment_id, r.n_opens, r.n_closes,
                     r.n_skipped, reason, r.error or "—")

    # Now spin up the daemon thread for ongoing ticks
    runner.start()
    log.info("Daemon thread started. Polling every %.0fs — Ctrl+C to stop.",
              args.poll)
    log.info("Watch this log: every tick prints heartbeat lines + any opens.")

    # ----- Embedded weekly rebaseline daemon ------------------------
    # Keeps the catalog cost-priced numbers fresh without needing a
    # separate LaunchAgent. Runs scripts/rebaseline_catalog.py
    # --safe-only every N days from inside this process. Default: 7d.
    if args.rebaseline_interval_days > 0:
        try:
            from core.embedded_rebaseline import start_rebaseline_thread
            start_rebaseline_thread(
                repo_dir=REPO,
                interval_days=args.rebaseline_interval_days,
                safe_only=True,
                enabled=True,
            )
            log.info("Rebaseline daemon started "
                      "(interval=%dd, safe-only). State file: "
                      "data/.last_rebaseline",
                      args.rebaseline_interval_days)
        except Exception as e:
            log.warning("Rebaseline daemon failed to start (non-fatal): %r",
                         e)
    else:
        log.info("Rebaseline daemon disabled "
                  "(--rebaseline-interval-days=0)")

    def _shutdown(_sig, _frm):
        log.info("Shutdown requested.")
        runner.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    last_event_count = len(runner.recent_events)
    try:
        while True:
            time.sleep(min(args.poll, 30.0))
            # Print every NEW event since last log (rather than just the
            # last one) so multiple ticks per minute are all visible.
            evts = runner.recent_events
            new_evts = evts[last_event_count:]
            for r in new_evts:
                level = logging.WARNING if r.error else logging.INFO
                reason = getattr(r, "skip_reason", "") or "—"
                log.log(level,
                         "%-50s opens=%d closes=%d skipped=%d (%s)  err=%r",
                         r.deployment_id, r.n_opens, r.n_closes,
                         r.n_skipped, reason, r.error or "—")
            last_event_count = len(evts)
            # Also print a heartbeat tick-time so you know the loop is alive
            if not new_evts:
                log.info("(idle — runner alive, last heartbeat=%s)",
                          runner.heartbeat_at_utc or "—")
    except KeyboardInterrupt:
        runner.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
