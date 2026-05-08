"""
daily_risk_accumulator.py — track and cap accumulated daily risk-at-open.

The problem this fixes
----------------------
Pre-this-module each deployment had its own `risk_pct` and
`max_money_risk_usd` cap, but there was NO account-wide accumulator.
If 5 deployments fired on a high-vol day at 0.30% risk each, total
exposure = 5 × $300 = $1,500 = 1.5% of $100k. If 4 of those stops
trigger, daily loss = 1.2% — already past the FTMO 0.5%-conservative /
1%-typical daily-loss cap with no blocker in the runner.

Symptom risk: FTMO daily-loss breach → account banned → entire
challenge fee + time investment lost.

The fix
-------
Sum the *risk-at-open* of every position opened today (UTC, with
configurable rollover hour). When a new signal wants to open and
its risk-at-open would push the daily total above
`daily_loss_cap_pct × starting_balance`, REFUSE the open.

What is "risk at open"?
-----------------------
The dollar amount that would be lost if the SL hits exactly. We
record it once at open-time:
   risk = lots × |entry - stop| × money_per_unit_per_lot
This is the SAME formula the position sizer uses to compute lots
from a target risk%. Recording it deterministically lets us compare
apples-to-apples across deployments.

Storage model
-------------
Per-account `data/accounts/<login>/daily_risk.json`:

    {
      "rollover_hhmm_utc": "22:00",      # FTMO daily reset
      "current_day_start_utc": "2026-05-05T22:00:00+00:00",
      "risk_used_usd": 873.45,
      "open_count": 3,
      "events": [
        {"at_utc": "...", "deployment_id": "...", "risk_usd": 300.0},
        ...
      ]
    }

Each tick the accumulator checks now() vs current_day_start_utc;
if a new day has started (now > current_day_start + 24h - rollover
adjustment), reset events[] and risk_used_usd to 0.

Concurrency
-----------
Single-runner architecture means at most ONE writer per account. We
use atomic write (tmp + rename) so concurrent dashboard reads see
consistent state. If we ever go multi-runner, this needs a SQLite
table not a JSON file.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Per-account in-memory locks so two threads can't race on the same
# account's daily-risk file. Single-runner per account but the
# dashboard might also write — lock guards both paths.
_locks: dict[int, threading.RLock] = {}


def _get_lock(login: int) -> threading.RLock:
    lk = _locks.get(login)
    if lk is None:
        lk = threading.RLock()
        _locks[login] = lk
    return lk


@dataclass
class DailyRiskState:
    """Mutable state — read+modified+written each open. Held in JSON
    on disk under the account dir. The accumulator wraps this so
    callers don't poke at the file directly."""
    rollover_hhmm_utc: str
    current_day_start_utc: str
    risk_used_usd: float
    open_count: int
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rollover_hhmm_utc": self.rollover_hhmm_utc,
            "current_day_start_utc": self.current_day_start_utc,
            "risk_used_usd": self.risk_used_usd,
            "open_count": self.open_count,
            "events": list(self.events),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DailyRiskState":
        return cls(
            rollover_hhmm_utc=str(d.get("rollover_hhmm_utc", "22:00")),
            current_day_start_utc=str(d.get("current_day_start_utc", "")),
            risk_used_usd=float(d.get("risk_used_usd", 0.0)),
            open_count=int(d.get("open_count", 0)),
            events=list(d.get("events", [])),
        )


def _state_path(login: int, accounts_dir: Optional[Path] = None) -> Path:
    """Where the daily-risk JSON lives for this account."""
    from core import account_manager
    if accounts_dir is None:
        accounts_dir = account_manager.ACCOUNTS_DIR
    return Path(accounts_dir) / str(login) / "daily_risk.json"


def _parse_hhmm(s: str) -> time:
    parts = s.split(":")
    return time(int(parts[0]), int(parts[1]))


def _current_ftmo_day_start(now_utc: datetime, rollover_hhmm: str) -> datetime:
    """Return the UTC datetime of the most recent FTMO-day rollover.

    FTMO resets daily P&L at 22:00 UTC. So at 21:59 UTC we're still on
    the previous trading day; at 22:00 a new day starts.
    """
    rt = _parse_hhmm(rollover_hhmm)
    todays_rollover = now_utc.replace(
        hour=rt.hour, minute=rt.minute,
        second=0, microsecond=0,
    )
    if now_utc < todays_rollover:
        # We haven't crossed today's rollover yet — current day started
        # YESTERDAY at the rollover time.
        return todays_rollover - timedelta(days=1)
    return todays_rollover


def _load_state(login: int, rollover_hhmm: str,
                accounts_dir: Optional[Path] = None) -> DailyRiskState:
    """Read the state file. Reset to fresh defaults if (a) file missing,
    (b) JSON corrupted, or (c) we've crossed into a new FTMO day since
    the file was last written."""
    path = _state_path(login, accounts_dir)
    now = datetime.now(timezone.utc)
    fresh_day_start = _current_ftmo_day_start(now, rollover_hhmm).isoformat()
    if not path.exists():
        return DailyRiskState(
            rollover_hhmm_utc=rollover_hhmm,
            current_day_start_utc=fresh_day_start,
            risk_used_usd=0.0,
            open_count=0,
        )
    try:
        body = path.read_text()
        data = json.loads(body)
        state = DailyRiskState.from_dict(data)
    except (OSError, json.JSONDecodeError, ValueError, TypeError) as e:
        log.warning("daily_risk: %s unreadable (%s) — resetting", path, e)
        return DailyRiskState(
            rollover_hhmm_utc=rollover_hhmm,
            current_day_start_utc=fresh_day_start,
            risk_used_usd=0.0,
            open_count=0,
        )
    # If a new FTMO day has started since the file was written, reset.
    if state.current_day_start_utc != fresh_day_start:
        log.info("daily_risk: rolled over to new FTMO day "
                 "(was %s, now %s); resetting accumulator",
                 state.current_day_start_utc, fresh_day_start)
        state = DailyRiskState(
            rollover_hhmm_utc=rollover_hhmm,
            current_day_start_utc=fresh_day_start,
            risk_used_usd=0.0,
            open_count=0,
        )
    return state


def _save_state(login: int, state: DailyRiskState,
                accounts_dir: Optional[Path] = None) -> None:
    """Atomic write: tmp + rename. Concurrent reads always see a
    valid file (either old or new — never partial)."""
    path = _state_path(login, accounts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2) + "\n")
    os.replace(tmp, path)


# ─── Public API ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    """Result of would_breach_cap. `would_breach`: caller must respect.
    `risk_used_usd_after`: what the running total would be if this open
    proceeded. `cap_usd`: the daily-loss cap converted to dollars."""
    would_breach: bool
    risk_used_usd_after: float
    cap_usd: float
    reason: str


def would_breach_cap(
    *,
    login: int,
    new_risk_usd: float,
    daily_loss_cap_pct: float,
    starting_balance_usd: float,
    rollover_hhmm: str = "22:00",
    accounts_dir: Optional[Path] = None,
) -> CheckResult:
    """Pre-flight check: would this new open push the daily total past
    the cap?

    Caller pattern:
        result = would_breach_cap(...)
        if result.would_breach:
            refuse_the_open(result.reason)
        else:
            send_the_order(...)
            record_open(...)   # registers the risk for accumulation
    """
    cap_usd = (daily_loss_cap_pct / 100.0) * starting_balance_usd
    with _get_lock(login):
        state = _load_state(login, rollover_hhmm, accounts_dir)
        after = state.risk_used_usd + new_risk_usd
        if after > cap_usd:
            return CheckResult(
                would_breach=True,
                risk_used_usd_after=after,
                cap_usd=cap_usd,
                reason=(
                    f"daily-risk cap would be breached: "
                    f"used=${state.risk_used_usd:.2f} + "
                    f"new=${new_risk_usd:.2f} = "
                    f"${after:.2f} > cap=${cap_usd:.2f} "
                    f"({daily_loss_cap_pct}% of "
                    f"${starting_balance_usd:,.0f})"
                ),
            )
        return CheckResult(
            would_breach=False,
            risk_used_usd_after=after,
            cap_usd=cap_usd,
            reason=(
                f"ok ({after:.2f} of {cap_usd:.2f} cap "
                f"= {after/cap_usd*100:.0f}%)"
            ),
        )


def record_open(
    *,
    login: int,
    deployment_id: str,
    risk_usd: float,
    rollover_hhmm: str = "22:00",
    accounts_dir: Optional[Path] = None,
    now_utc: Optional[datetime] = None,
) -> DailyRiskState:
    """Register a successfully-opened position's risk-at-open against
    the day's accumulator. Atomic update. Returns the new state.

    Call this AFTER the broker confirms the open — not before.
    Pre-confirm calls would over-accumulate if the open later fails.
    """
    when = now_utc or datetime.now(timezone.utc)
    with _get_lock(login):
        state = _load_state(login, rollover_hhmm, accounts_dir)
        state.risk_used_usd += float(risk_usd)
        state.open_count += 1
        state.events.append({
            "at_utc": when.isoformat(),
            "deployment_id": deployment_id,
            "risk_usd": float(risk_usd),
        })
        # Cap event log to last 100 entries to keep the JSON small.
        if len(state.events) > 100:
            state.events = state.events[-100:]
        _save_state(login, state, accounts_dir)
        log.info(
            "daily_risk: +$%.2f from %s — total $%.2f / %d opens "
            "(day started %s)",
            risk_usd, deployment_id, state.risk_used_usd,
            state.open_count, state.current_day_start_utc,
        )
        return state


def get_summary(
    login: int,
    daily_loss_cap_pct: float,
    starting_balance_usd: float,
    rollover_hhmm: str = "22:00",
    accounts_dir: Optional[Path] = None,
) -> dict:
    """Read-only snapshot for the dashboard. Doesn't mutate state."""
    cap_usd = (daily_loss_cap_pct / 100.0) * starting_balance_usd
    with _get_lock(login):
        state = _load_state(login, rollover_hhmm, accounts_dir)
        return {
            "current_day_start_utc": state.current_day_start_utc,
            "risk_used_usd": state.risk_used_usd,
            "open_count": state.open_count,
            "cap_usd": cap_usd,
            "daily_loss_cap_pct": daily_loss_cap_pct,
            "fraction_used": (state.risk_used_usd / cap_usd
                                if cap_usd > 0 else 0.0),
            "remaining_usd": max(0.0, cap_usd - state.risk_used_usd),
            "events": state.events[-10:],
        }
