"""
embedded_rebaseline.py — run rebaseline_catalog.py weekly from inside
the live runner process (instead of via a separate LaunchAgent).

Rationale: the user runs `make run-live` (= scripts/run_deployments.py)
as the single long-lived entry point. They don't install separate
LaunchAgents. So the catalog rebaseline needs to live INSIDE that
runner process.

Design:
  • A daemon thread that wakes every CHECK_INTERVAL_SEC (default 1h)
    and asks "has it been ≥ INTERVAL_DAYS since last rebaseline?"
  • If yes, spawn a subprocess to run scripts/rebaseline_catalog.py
    with `--safe-only` (so it only re-runs cells that already passed
    the gate — keeps it fast: ~2-3 min instead of ~15 min full).
  • Records last-run timestamp in `data/.last_rebaseline` so the
    schedule survives runner restarts.
  • Logs go to stdout of the runner process so the user sees them
    in their normal `make run-live` log stream.

This is intentionally simple — no APScheduler, no cron emulation.
The runner is single-threaded for live logic; this is a daemon
sidecar that won't slow ticks.

Wiring (in scripts/run_deployments.py):
    from core.embedded_rebaseline import start_rebaseline_thread
    start_rebaseline_thread(repo_dir=REPO)

That's it. The thread auto-shuts-down when the parent process exits
(daemon=True).
"""
from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# How often the daemon wakes to check the schedule. 1 hour is plenty
# — we're scheduling on a weekly cadence, not minute-precise.
CHECK_INTERVAL_SEC = 60 * 60         # 1 hour

# How many days between rebaselines. 7 = weekly, matching the prior
# LaunchAgent schedule. Bump down to 3 for more aggressive drift
# tracking, or up to 14 to save compute.
DEFAULT_INTERVAL_DAYS = 7

# Subprocess timeout. Rebaseline-safe-only typically runs in 2-3 min;
# 30 min is a generous ceiling that catches a hang without killing a
# legitimately slow run.
SUBPROCESS_TIMEOUT_SEC = 30 * 60


@dataclass(frozen=True)
class RebaselineConfig:
    repo_dir: Path
    interval_days: int = DEFAULT_INTERVAL_DAYS
    state_file: Path | None = None      # default: repo/data/.last_rebaseline
    safe_only: bool = True
    enabled: bool = True

    @property
    def resolved_state_file(self) -> Path:
        if self.state_file is not None:
            return self.state_file
        return self.repo_dir / "data" / ".last_rebaseline"


def _read_last_run(state_file: Path) -> datetime | None:
    if not state_file.exists():
        return None
    try:
        text = state_file.read_text().strip()
        return datetime.fromisoformat(text)
    except (ValueError, OSError):
        return None


def _write_last_run(state_file: Path, when: datetime) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(when.isoformat())


def is_due(cfg: RebaselineConfig, *, now: datetime | None = None) -> bool:
    """Return True if a rebaseline is due (last run >= interval ago,
    or never run).
    """
    now = now or datetime.now(timezone.utc)
    last = _read_last_run(cfg.resolved_state_file)
    if last is None:
        return True
    age_days = (now - last).total_seconds() / 86400.0
    return age_days >= cfg.interval_days


def run_rebaseline_once(cfg: RebaselineConfig) -> bool:
    """Spawn the rebaseline subprocess. Returns True on clean exit.

    Uses the venv's python so no PATH mishaps. Captures and forwards
    stdout/stderr through Python logging so output lands in the
    runner's log stream alongside trade events.
    """
    pybin = cfg.repo_dir / ".venv" / "bin" / "python"
    if not pybin.exists():
        pybin = cfg.repo_dir / ".venv" / "Scripts" / "python.exe"
    if not pybin.exists():
        log.warning("rebaseline: venv python not found at %s — skipping",
                     pybin)
        return False
    script = cfg.repo_dir / "scripts" / "rebaseline_catalog.py"
    if not script.exists():
        log.warning("rebaseline: script missing at %s — skipping", script)
        return False

    args = [str(pybin), str(script)]
    if cfg.safe_only:
        args.append("--safe-only")

    log.info("rebaseline: starting (%s)", " ".join(args[1:]))
    t0 = time.time()
    try:
        result = subprocess.run(
            args,
            cwd=str(cfg.repo_dir),
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        log.error("rebaseline: TIMEOUT after %.0fs — killed",
                   SUBPROCESS_TIMEOUT_SEC)
        return False
    except Exception as e:
        log.exception("rebaseline: failed to spawn: %r", e)
        return False

    elapsed = time.time() - t0
    # Tail the last few lines so the runner log shows the summary.
    tail_n = 8
    out_tail = "\n".join(result.stdout.strip().splitlines()[-tail_n:])
    err_tail = "\n".join(result.stderr.strip().splitlines()[-tail_n:])
    if result.returncode == 0:
        log.info("rebaseline: ✅ done in %.0fs\n%s", elapsed, out_tail)
        _write_last_run(cfg.resolved_state_file,
                         datetime.now(timezone.utc))
        return True
    else:
        log.error("rebaseline: ❌ exit=%d in %.0fs\nSTDOUT tail:\n%s\n"
                   "STDERR tail:\n%s",
                   result.returncode, elapsed, out_tail, err_tail)
        return False


def _daemon_loop(cfg: RebaselineConfig) -> None:
    """Forever: every hour, check if a rebaseline is due, run if yes."""
    log.info("rebaseline-daemon: started (interval=%dd, safe_only=%s, "
              "state=%s)",
              cfg.interval_days, cfg.safe_only, cfg.resolved_state_file)
    while True:
        try:
            if is_due(cfg):
                run_rebaseline_once(cfg)
            else:
                last = _read_last_run(cfg.resolved_state_file)
                next_due_in_hr = max(
                    0,
                    (cfg.interval_days * 24)
                    - (datetime.now(timezone.utc)
                       - last).total_seconds() / 3600.0,
                ) if last else 0
                log.debug("rebaseline-daemon: not due "
                            "(next in ~%.1fh)", next_due_in_hr)
        except Exception as e:
            log.exception("rebaseline-daemon: tick failed: %r", e)
        time.sleep(CHECK_INTERVAL_SEC)


def start_rebaseline_thread(*,
                              repo_dir: Path,
                              interval_days: int = DEFAULT_INTERVAL_DAYS,
                              safe_only: bool = True,
                              enabled: bool = True,
                              ) -> threading.Thread | None:
    """Start the daemon thread. Returns the Thread (or None if
    disabled). Caller doesn't need to stop it — daemon=True means it
    dies with the parent process.
    """
    if not enabled:
        log.info("rebaseline-daemon: disabled by config")
        return None
    cfg = RebaselineConfig(
        repo_dir=Path(repo_dir),
        interval_days=interval_days,
        safe_only=safe_only,
        enabled=True,
    )
    t = threading.Thread(
        target=_daemon_loop, args=(cfg,),
        name="rebaseline-daemon", daemon=True,
    )
    t.start()
    return t
