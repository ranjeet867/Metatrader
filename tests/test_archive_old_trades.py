"""
test_archive_old_trades.py — verify the hot/cold split correctly
moves old rows from v2.db → v2_archive.db without losing data.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _make_hot_db(path: Path) -> None:
    """Create a v2.db-shaped hot DB with `trades` and synthetic rows."""
    with sqlite3.connect(str(path)) as conn:
        conn.executescript("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                run_id TEXT, trade_idx INTEGER, symbol TEXT,
                direction TEXT, opened_at_utc TEXT,
                closed_at_utc TEXT, entry_price REAL, stop_price REAL,
                target_price REAL, exit_price REAL, lots REAL,
                realized_pnl REAL, r_multiple REAL,
                close_reason TEXT, mode TEXT, strategy TEXT, tf TEXT
            );
            CREATE TABLE forensic_snapshots (
                id INTEGER PRIMARY KEY,
                captured_at_utc TEXT, login INTEGER, equity_usd REAL
            );
        """)
        # 5 trades from 1 year ago, 3 from yesterday
        old_ts = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        new_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        for i in range(5):
            conn.execute(
                """INSERT INTO trades
                   (run_id, trade_idx, symbol, direction, opened_at_utc,
                    closed_at_utc, entry_price, stop_price, target_price,
                    exit_price, lots, realized_pnl, r_multiple,
                    close_reason, mode, strategy, tf)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"r{i}", 0, "EURUSD", "LONG", old_ts, old_ts,
                 1.10, 1.09, 1.12, 1.115, 1.0, 50.0, 1.5,
                 "tp", "live", "rsi_30_70", "M15"),
            )
        for i in range(3):
            conn.execute(
                """INSERT INTO trades
                   (run_id, trade_idx, symbol, direction, opened_at_utc,
                    closed_at_utc, entry_price, stop_price, target_price,
                    exit_price, lots, realized_pnl, r_multiple,
                    close_reason, mode, strategy, tf)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"r{10 + i}", 0, "EURUSD", "LONG", new_ts, new_ts,
                 1.10, 1.09, 1.12, 1.115, 1.0, 50.0, 1.5,
                 "tp", "live", "rsi_30_70", "M15"),
            )
        conn.commit()


def test_dry_run_does_not_move_anything(tmp_path):
    hot = tmp_path / "v2.db"
    archive = tmp_path / "v2_archive.db"
    _make_hot_db(hot)

    from scripts.archive_old_trades import archive_old_rows
    counts = archive_old_rows(hot, archive, keep_days=90, apply=False)
    # Dry-run reports counts but doesn't actually move
    assert counts["trades"] == 5  # 5 old rows would be moved
    # Hot still has all 8
    with sqlite3.connect(str(hot)) as c:
        n = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n == 8


def test_apply_moves_old_rows_only(tmp_path):
    hot = tmp_path / "v2.db"
    archive = tmp_path / "v2_archive.db"
    _make_hot_db(hot)

    from scripts.archive_old_trades import archive_old_rows
    counts = archive_old_rows(hot, archive, keep_days=90, apply=True)
    assert counts["trades"] == 5

    # Hot now has only 3 (recent) trades
    with sqlite3.connect(str(hot)) as c:
        n_hot = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n_hot == 3
    # Archive has 5
    with sqlite3.connect(str(archive)) as c:
        n_arc = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert n_arc == 5


def test_apply_is_idempotent(tmp_path):
    hot = tmp_path / "v2.db"
    archive = tmp_path / "v2_archive.db"
    _make_hot_db(hot)

    from scripts.archive_old_trades import archive_old_rows
    archive_old_rows(hot, archive, keep_days=90, apply=True)
    counts2 = archive_old_rows(hot, archive, keep_days=90, apply=True)
    # Second run finds nothing to move (all old rows already archived)
    assert counts2["trades"] == 0


def test_handles_missing_table_gracefully(tmp_path):
    hot = tmp_path / "v2.db"
    archive = tmp_path / "v2_archive.db"
    # Create hot DB with only `trades`, no forensic_snapshots
    with sqlite3.connect(str(hot)) as conn:
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                run_id TEXT, trade_idx INTEGER, symbol TEXT,
                direction TEXT, opened_at_utc TEXT,
                closed_at_utc TEXT, entry_price REAL, stop_price REAL,
                target_price REAL, exit_price REAL, lots REAL,
                realized_pnl REAL, r_multiple REAL,
                close_reason TEXT, mode TEXT, strategy TEXT, tf TEXT
            );
        """)
        conn.commit()

    from scripts.archive_old_trades import archive_old_rows
    counts = archive_old_rows(hot, archive, keep_days=30, apply=True)
    # forensic_snapshots not in hot → reported as 0
    assert counts["forensic_snapshots"] == 0
    assert counts["trades"] == 0
