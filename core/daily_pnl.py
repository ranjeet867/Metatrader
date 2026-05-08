"""
daily_pnl.py — compute today's realized P&L since the FTMO daily reset.

Why this matters
----------------
FTMO measures daily-loss against the account state at the daily reset
hour (22:00 UTC by default). Today's realized P&L = sum of realized
P&L from every trade CLOSED since that reset. This is what
adaptive_risk needs to size today's remaining trades against the
remaining-buffer.

Source of truth
---------------
v2.db `trades` table — every closed trade has `realized_pnl` and
`closed_at_utc`. We sum across all trades closed at-or-after today's
rollover.

Open positions don't count here — they have unrealized P&L which is
a different concept. FTMO's daily-loss limit is the realized side.
The runner's `position_guard` etc. handle open exposure separately.

Pure function — no I/O beyond a single SQLite read.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _parse_hhmm(s: str) -> time:
    """'22:00' → time(22, 0)."""
    parts = s.split(":")
    return time(int(parts[0]), int(parts[1]))


def current_ftmo_day_start_utc(
    now_utc: datetime, rollover_hhmm: str = "22:00",
) -> datetime:
    """The most recent FTMO-day rollover datetime (UTC).

    Same logic as `daily_risk_accumulator._current_ftmo_day_start`.
    Pulled into its own helper so daily_pnl can reuse it without an
    accumulator-coupling.
    """
    rt = _parse_hhmm(rollover_hhmm)
    todays_rollover = now_utc.replace(
        hour=rt.hour, minute=rt.minute,
        second=0, microsecond=0,
    )
    if now_utc < todays_rollover:
        return todays_rollover - timedelta(days=1)
    return todays_rollover


def realized_pnl_today(
    db_path: Path | str,
    *,
    rollover_hhmm: str = "22:00",
    now_utc: Optional[datetime] = None,
) -> float:
    """Sum of `realized_pnl` from every trade closed since today's
    FTMO daily rollover. Returns a float (negative when net-losing).

    Pure read — no mutation. Returns 0.0 on any error.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return 0.0
    when = now_utc or datetime.now(timezone.utc)
    day_start = current_ftmo_day_start_utc(when, rollover_hhmm)
    cutoff = day_start.isoformat()
    try:
        with sqlite3.connect(str(db_path)) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0.0) "
                "FROM trades "
                "WHERE closed_at_utc IS NOT NULL "
                "  AND closed_at_utc >= ?",
                (cutoff,),
            ).fetchone()
            return float(row[0]) if row else 0.0
    except Exception as e:
        log.warning("daily_pnl: query failed (%s) — returning 0.0", e)
        return 0.0


def realized_pnl_today_summary(
    db_path: Path | str,
    *,
    rollover_hhmm: str = "22:00",
    now_utc: Optional[datetime] = None,
) -> dict:
    """Return a dict with today's realized P&L + breakdown.

    Used by the dashboard tile that shows "today: -$215 across 3 trades"
    next to the adaptive-risk computation.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return {"total_pnl": 0.0, "n_trades": 0, "n_wins": 0,
                "n_losses": 0, "day_start_utc": None}
    when = now_utc or datetime.now(timezone.utc)
    day_start = current_ftmo_day_start_utc(when, rollover_hhmm)
    cutoff = day_start.isoformat()
    try:
        with sqlite3.connect(str(db_path)) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT realized_pnl FROM trades "
                "WHERE closed_at_utc IS NOT NULL "
                "  AND closed_at_utc >= ?",
                (cutoff,),
            ).fetchall()
            total = sum(float(r["realized_pnl"] or 0.0) for r in rows)
            n_w = sum(1 for r in rows if (r["realized_pnl"] or 0.0) > 0)
            n_l = sum(1 for r in rows if (r["realized_pnl"] or 0.0) < 0)
            return {
                "total_pnl": total,
                "n_trades": len(rows),
                "n_wins": n_w,
                "n_losses": n_l,
                "day_start_utc": day_start.isoformat(),
            }
    except Exception as e:
        log.warning("daily_pnl_summary: query failed (%s)", e)
        return {"total_pnl": 0.0, "n_trades": 0, "n_wins": 0,
                "n_losses": 0, "day_start_utc": None}
