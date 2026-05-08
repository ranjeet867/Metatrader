"""
test_forensic_snapshot.py — verify the periodic state-capture module.

Coverage:
1. Schema bootstrap on first call
2. Insert + read-back round-trip preserves all fields
3. Time-range query filters correctly
4. Prune removes old rows, keeps recent
5. Empty / missing DB → returns empty list, no crash
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from core import forensic_snapshot as fs


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_forensic.db"


def test_capture_creates_schema_on_first_call(tmp_db):
    assert not tmp_db.exists()
    fs.capture(tmp_db, login=12345,
                  equity_usd=100_000.0, balance_usd=100_000.0)
    assert tmp_db.exists()


def test_capture_round_trip_preserves_fields(tmp_db):
    ts = "2026-05-05T15:00:00+00:00"
    fs.capture(
        tmp_db, login=99999,
        equity_usd=91_408.50,
        balance_usd=92_000.00,
        n_open_positions=3,
        daily_realized_pnl_usd=-150.25,
        daily_unrealized_pnl_usd=42.10,
        ftmo_buffer_remaining_pct=2.34,
        equity_regime="fragile_recovery",
        active_deployments_count=7,
        emergency_stop_active=False,
        notes={"trigger": "JP225 TSMOM signal", "extra": 42},
        captured_at_utc=ts,
    )
    rows = fs.fetch_recent(tmp_db, login=99999, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r.captured_at_utc == ts
    assert r.login == 99999
    assert abs(r.equity_usd - 91_408.50) < 0.01
    assert abs(r.balance_usd - 92_000.00) < 0.01
    assert r.n_open_positions == 3
    assert abs(r.daily_realized_pnl_usd + 150.25) < 0.01
    assert abs(r.daily_unrealized_pnl_usd - 42.10) < 0.01
    assert abs(r.ftmo_buffer_remaining_pct - 2.34) < 0.01
    assert r.equity_regime == "fragile_recovery"
    assert r.active_deployments_count == 7
    assert r.emergency_stop_active is False
    assert r.notes["trigger"] == "JP225 TSMOM signal"
    assert r.notes["extra"] == 42


def test_fetch_recent_orders_newest_first(tmp_db):
    base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    for i in range(5):
        ts = (base + _dt.timedelta(minutes=i)).isoformat()
        fs.capture(tmp_db, login=1, equity_usd=100_000 + i,
                      captured_at_utc=ts)
    rows = fs.fetch_recent(tmp_db, login=1, limit=10)
    assert len(rows) == 5
    # Newest first → equity_usd descending (100004 first, 100000 last)
    assert rows[0].equity_usd == 100_004
    assert rows[-1].equity_usd == 100_000


def test_fetch_recent_filters_by_login(tmp_db):
    fs.capture(tmp_db, login=1, equity_usd=1.0,
                  captured_at_utc="2026-01-01T00:00:00+00:00")
    fs.capture(tmp_db, login=2, equity_usd=2.0,
                  captured_at_utc="2026-01-01T00:00:00+00:00")
    rows1 = fs.fetch_recent(tmp_db, login=1)
    rows2 = fs.fetch_recent(tmp_db, login=2)
    assert len(rows1) == 1
    assert len(rows2) == 1
    assert rows1[0].equity_usd == 1.0
    assert rows2[0].equity_usd == 2.0


def test_fetch_range_filters_by_time(tmp_db):
    base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    for i in range(10):
        ts = (base + _dt.timedelta(hours=i)).isoformat()
        fs.capture(tmp_db, login=1, equity_usd=100_000 + i,
                      captured_at_utc=ts)
    # Query a 3-hour window in the middle
    start = (base + _dt.timedelta(hours=3)).isoformat()
    end = (base + _dt.timedelta(hours=5)).isoformat()
    rows = fs.fetch_range(tmp_db, login=1,
                              start_utc=start, end_utc=end)
    # Should get hours 3, 4, 5 → 3 rows
    assert len(rows) == 3
    # Range query returns oldest-first
    assert rows[0].equity_usd == 100_003
    assert rows[-1].equity_usd == 100_005


def test_prune_removes_old_rows(tmp_db):
    # Two rows: one ancient (1 year ago), one fresh (today)
    ancient_ts = "2024-01-01T00:00:00+00:00"
    today_ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    fs.capture(tmp_db, login=1, equity_usd=1.0,
                  captured_at_utc=ancient_ts)
    fs.capture(tmp_db, login=1, equity_usd=2.0,
                  captured_at_utc=today_ts)

    # Keep last 30 days only — ancient row should be pruned
    deleted = fs.prune_older_than(tmp_db, keep_days=30)
    assert deleted == 1
    # Only the fresh row remains
    rows = fs.fetch_recent(tmp_db, login=1)
    assert len(rows) == 1
    assert rows[0].equity_usd == 2.0


def test_fetch_recent_missing_db_returns_empty(tmp_db):
    # File doesn't exist
    rows = fs.fetch_recent(tmp_db, login=1)
    assert rows == []


def test_fetch_range_missing_db_returns_empty(tmp_db):
    rows = fs.fetch_range(tmp_db, login=1,
                              start_utc="2020-01-01T00:00:00+00:00",
                              end_utc="2030-01-01T00:00:00+00:00")
    assert rows == []


def test_capture_handles_none_optional_fields(tmp_db):
    """Some fields are Optional — verify None is stored & read correctly."""
    fs.capture(tmp_db, login=1, captured_at_utc="2026-01-01T00:00:00+00:00")
    rows = fs.fetch_recent(tmp_db, login=1)
    assert len(rows) == 1
    r = rows[0]
    assert r.equity_usd is None
    assert r.balance_usd is None
    assert r.equity_regime is None
    assert r.ftmo_buffer_remaining_pct is None
    assert r.notes == {}
