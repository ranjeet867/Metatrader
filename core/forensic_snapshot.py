"""
forensic_snapshot.py — periodic system-state capture for post-mortem.

Why this exists
---------------
When something goes wrong (cell decays unexpectedly, equity drops
without explanation, deployment misbehaves), the only way to know
WHY is to have a time-series of what the system saw at each moment.
The journal records trades. The bridge_events table records
interactions with MT5. But neither captures the *interpretation* of
the system at each tick — what equity it observed, how many positions
were open, what daily P&L was, what regime the equity tracker said
we were in.

This module captures all of that into one append-only table at a
configurable cadence (default 60s). Running for a year produces
~500k rows = ~30 MB of SQLite — bounded growth.

Schema
------
forensic_snapshots:
  id, captured_at_utc, login,
  equity_usd, balance_usd,
  n_open_positions, n_paper_executors_with_position,
  daily_realized_pnl_usd, daily_unrealized_pnl_usd,
  ftmo_buffer_remaining_pct,
  equity_regime,
  active_deployments_count,
  emergency_stop_active,
  notes_json (dict for future-extension)

What you'd ask of it
--------------------
- "Show me equity + open-position-count for the day rsi_30_70 lost $400"
  → query by captured_at_utc range, rebuild the timeline
- "Did the equity regime change before or after the JP225 trade opened?"
  → join forensic_snapshots with trades on opened_at_utc
- "How often have we hit ftmo_buffer < 1%?"
  → simple aggregate over the table

Caller pattern
--------------
The DeploymentRunner main loop calls
``forensic_snapshot.capture(self)`` once per minute (or whatever the
configured cadence is). Implementation is bounded-cost: one INSERT
per call, ~1ms.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS forensic_snapshots (
        id                              INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at_utc                 TEXT    NOT NULL,
        login                           INTEGER NOT NULL,
        equity_usd                      REAL,
        balance_usd                     REAL,
        n_open_positions                INTEGER,
        daily_realized_pnl_usd          REAL,
        daily_unrealized_pnl_usd        REAL,
        ftmo_buffer_remaining_pct       REAL,
        equity_regime                   TEXT,
        active_deployments_count        INTEGER,
        emergency_stop_active           INTEGER,
        notes_json                      TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_forensic_login_time
        ON forensic_snapshots(login, captured_at_utc DESC);
"""


@dataclass
class Snapshot:
    """Typed view of one snapshot row. Used by readers (Performance
    page, audit scripts) so the schema is documented in code."""
    captured_at_utc: str
    login: int
    equity_usd: Optional[float]
    balance_usd: Optional[float]
    n_open_positions: int
    daily_realized_pnl_usd: float
    daily_unrealized_pnl_usd: float
    ftmo_buffer_remaining_pct: Optional[float]
    equity_regime: Optional[str]
    active_deployments_count: int
    emergency_stop_active: bool
    notes: dict[str, Any]


def _ensure_schema(db_path: Path) -> None:
    """Idempotent schema bootstrap."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA_DDL)
        conn.commit()


def capture(
    db_path: Path,
    *,
    login: int,
    equity_usd: Optional[float] = None,
    balance_usd: Optional[float] = None,
    n_open_positions: int = 0,
    daily_realized_pnl_usd: float = 0.0,
    daily_unrealized_pnl_usd: float = 0.0,
    ftmo_buffer_remaining_pct: Optional[float] = None,
    equity_regime: Optional[str] = None,
    active_deployments_count: int = 0,
    emergency_stop_active: bool = False,
    notes: Optional[dict[str, Any]] = None,
    captured_at_utc: Optional[str] = None,
) -> None:
    """Append one snapshot row.

    Wrapped in try/except by callers so a forensic-write failure
    NEVER blocks the runner's main loop. This function intentionally
    raises on programmer errors (bad type) so the test suite catches
    them, but should be called inside try/except in production code.
    """
    _ensure_schema(db_path)
    if captured_at_utc is None:
        captured_at_utc = datetime.now(timezone.utc).isoformat()
    notes_json = json.dumps(notes) if notes else None

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO forensic_snapshots
                (captured_at_utc, login, equity_usd, balance_usd,
                 n_open_positions, daily_realized_pnl_usd,
                 daily_unrealized_pnl_usd,
                 ftmo_buffer_remaining_pct, equity_regime,
                 active_deployments_count, emergency_stop_active,
                 notes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                captured_at_utc, int(login),
                equity_usd, balance_usd,
                int(n_open_positions),
                float(daily_realized_pnl_usd),
                float(daily_unrealized_pnl_usd),
                ftmo_buffer_remaining_pct, equity_regime,
                int(active_deployments_count),
                int(bool(emergency_stop_active)),
                notes_json,
            ),
        )
        conn.commit()


def fetch_recent(db_path: Path, *, login: int,
                    limit: int = 1000) -> list[Snapshot]:
    """Read recent snapshots (newest-first) for one login."""
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM forensic_snapshots
                 WHERE login = ?
                 ORDER BY captured_at_utc DESC, id DESC
                 LIMIT ?
                """,
                (int(login), int(limit)),
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def fetch_range(db_path: Path, *, login: int,
                   start_utc: str, end_utc: str) -> list[Snapshot]:
    """Read snapshots within a time range (oldest-first), for
    post-mortem reconstruction."""
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM forensic_snapshots
                 WHERE login = ?
                   AND captured_at_utc >= ?
                   AND captured_at_utc <= ?
                 ORDER BY captured_at_utc ASC, id ASC
                """,
                (int(login), start_utc, end_utc),
            ).fetchall()
        return [_row_to_snapshot(r) for r in rows]
    except sqlite3.OperationalError:
        return []


def _row_to_snapshot(row) -> Snapshot:
    notes = {}
    if row["notes_json"]:
        try:
            notes = json.loads(row["notes_json"])
        except (ValueError, TypeError):
            notes = {}
    return Snapshot(
        captured_at_utc=row["captured_at_utc"],
        login=int(row["login"]),
        equity_usd=row["equity_usd"],
        balance_usd=row["balance_usd"],
        n_open_positions=int(row["n_open_positions"] or 0),
        daily_realized_pnl_usd=float(row["daily_realized_pnl_usd"] or 0),
        daily_unrealized_pnl_usd=float(
            row["daily_unrealized_pnl_usd"] or 0),
        ftmo_buffer_remaining_pct=row["ftmo_buffer_remaining_pct"],
        equity_regime=row["equity_regime"],
        active_deployments_count=int(
            row["active_deployments_count"] or 0),
        emergency_stop_active=bool(row["emergency_stop_active"]),
        notes=notes,
    )


def prune_older_than(db_path: Path, *, keep_days: int = 365) -> int:
    """Remove snapshots older than `keep_days`. Returns rows deleted.

    Annual cleanup keeps the table bounded: 60s cadence × 365 days
    × 24h × 60min ≈ 525k rows ≈ 30 MB. Trim to keep ~1y of forensic
    history."""
    if not db_path.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
    cutoff_iso = datetime.fromtimestamp(
        cutoff, tz=timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "DELETE FROM forensic_snapshots WHERE captured_at_utc < ?",
                (cutoff_iso,),
            )
            conn.commit()
            return int(cur.rowcount or 0)
    except sqlite3.OperationalError:
        return 0
