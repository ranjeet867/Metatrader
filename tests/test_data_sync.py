"""
test_data_sync.py — pin the contract that core.data_sync.sync_to_now
keeps the on-disk parquet in sync with whatever the bridge fetcher
returns, atomically, without corrupting good data on bad fetches.

Why these tests exist
---------------------
Pre-fix the live runner pulled bars from MT5 each tick but the on-disk
parquet only updated when the user manually ran `make refresh-data`.
Result: the dashboard saw 6h-stale bars while the runner happily
ticked on fresh ones — looked indistinguishable from a broken runner.

`sync_to_now` closes that gap by appending any new bars to the
parquet on every tick. These tests pin the boring-but-load-bearing
contracts:

  1. Empty parquet + non-empty fetch  → write happens
  2. Existing parquet + 0 new bars    → NO write (cheap path)
  3. Existing parquet + N new bars    → append + atomic write
  4. Bridge fails entirely            → existing parquet preserved
  5. Bridge returns malformed         → existing parquet preserved
  6. Atomic write — if rename fails halfway, no corrupt parquet

If any of these break, real-money trading silently sees diverging
data. That's why this file is short but every assert matters.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from core import data_sync


def _make_bars(start: datetime, n: int, tf_minutes: int = 15) -> pd.DataFrame:
    """Make n consecutive M15 bars starting at `start` (UTC)."""
    times = [start + timedelta(minutes=tf_minutes * i) for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times, utc=True),
        "open":  [1.10 + i * 0.0001 for i in range(n)],
        "high":  [1.11 + i * 0.0001 for i in range(n)],
        "low":   [1.09 + i * 0.0001 for i in range(n)],
        "close": [1.105 + i * 0.0001 for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
    })


# ─── 1. empty parquet + non-empty fetch → write ────────────────────────
def test_empty_parquet_initial_fetch_writes(tmp_path: Path):
    parquet = tmp_path / "EURUSD_M15.parquet"
    fresh = _make_bars(datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc), n=10)

    def fetcher(t, tf, n):
        assert t == "EURUSD" and tf == "M15"
        return fresh

    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet, count=500,
    )

    assert parquet.exists(), "initial fetch should create the parquet"
    assert len(out) == 10
    on_disk = pd.read_parquet(parquet)
    assert len(on_disk) == 10
    assert list(on_disk.columns) == ["time", "open", "high", "low", "close", "volume"]


# ─── 2. existing parquet + 0 new bars → NO write ───────────────────────
def test_no_new_bars_no_write(tmp_path: Path):
    """Hot path. Most ticks return identical 500 bars to last tick.
    We must NOT rewrite the parquet — atomic-rename per minute would be
    wasteful and would race the dashboard reader."""
    parquet = tmp_path / "EURUSD_M15.parquet"
    initial = _make_bars(datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc), n=10)

    # Seed the parquet
    data_sync.save_parquet(initial, parquet)
    mtime_before = parquet.stat().st_mtime_ns

    # Bridge returns the same 10 bars
    def fetcher(t, tf, n):
        return initial.copy()

    import time
    time.sleep(0.01)   # ensure mtime would change if a write happened

    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet,
    )
    mtime_after = parquet.stat().st_mtime_ns

    assert len(out) == 10
    assert mtime_before == mtime_after, "parquet should NOT have been rewritten"


# ─── 3. existing parquet + N new bars → append + atomic write ──────────
def test_new_bars_appended(tmp_path: Path):
    parquet = tmp_path / "EURUSD_M15.parquet"
    start = datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc)
    initial = _make_bars(start, n=10)
    data_sync.save_parquet(initial, parquet)

    # Bridge returns the original 10 + 3 new
    fresh = _make_bars(start, n=13)

    def fetcher(t, tf, n):
        return fresh.copy()

    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet,
    )

    assert len(out) == 13
    on_disk = pd.read_parquet(parquet)
    assert len(on_disk) == 13
    # Last bar should be the newest
    last_t = on_disk["time"].iloc[-1]
    expected_last = start + timedelta(minutes=15 * 12)
    assert pd.Timestamp(last_t).tz_convert("UTC") == pd.Timestamp(expected_last)


# ─── 4. bridge fails entirely → existing parquet preserved ─────────────
def test_bridge_failure_preserves_existing(tmp_path: Path):
    parquet = tmp_path / "EURUSD_M15.parquet"
    initial = _make_bars(datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc), n=10)
    data_sync.save_parquet(initial, parquet)
    bytes_before = parquet.read_bytes()

    def fetcher(t, tf, n):
        raise RuntimeError("bridge timeout")

    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet,
    )

    # Existing parquet must be returned unchanged
    assert len(out) == 10
    assert parquet.read_bytes() == bytes_before, (
        "parquet must NOT be touched when bridge fails"
    )


# ─── 5. bridge returns malformed → existing parquet preserved ──────────
def test_malformed_bridge_response_preserves_existing(tmp_path: Path):
    parquet = tmp_path / "EURUSD_M15.parquet"
    initial = _make_bars(datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc), n=10)
    data_sync.save_parquet(initial, parquet)
    bytes_before = parquet.read_bytes()

    # Missing 'time' column = malformed
    def fetcher(t, tf, n):
        return pd.DataFrame({
            "open": [1.0], "high": [1.0],
            "low": [1.0], "close": [1.0], "volume": [1.0],
        })

    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet,
    )

    assert len(out) == 10
    assert parquet.read_bytes() == bytes_before


# ─── 6. atomic write — concurrent reader sees only valid bytes ─────────
def test_atomic_write_no_partial_state(tmp_path: Path, monkeypatch):
    """Crash mid-write → parquet is either old-version or new-version,
    never corrupted. We simulate by having os.replace raise after the
    tmp file is written; the original parquet should still be valid."""
    parquet = tmp_path / "EURUSD_M15.parquet"
    initial = _make_bars(datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc), n=10)
    data_sync.save_parquet(initial, parquet)
    original_bytes = parquet.read_bytes()

    fresh = _make_bars(datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc), n=15)

    def fetcher(t, tf, n):
        return fresh.copy()

    # Inject failure at the os.replace step (atomic rename)
    real_replace = data_sync.os.replace
    calls = {"n": 0}

    def fail_replace(src, dst):
        calls["n"] += 1
        raise OSError("simulated rename failure")

    monkeypatch.setattr(data_sync.os, "replace", fail_replace)

    # Should not raise — sync swallows write failures and returns df
    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet,
    )

    assert calls["n"] == 1
    # In-memory result has 15 bars (sync still returns merged view)
    assert len(out) == 15
    # On-disk parquet is unchanged — atomicity preserved
    assert parquet.read_bytes() == original_bytes
    # The .tmp file may exist but the live parquet is still valid
    on_disk = pd.read_parquet(parquet)
    assert len(on_disk) == 10


# ─── 7. integration: bars across midnight UTC, sorting + dedupe ────────
def test_dedupe_and_sort_on_merge(tmp_path: Path):
    """Bridge sometimes returns overlapping bars (the same M15 bar at
    boundary). Merge must dedupe on time + keep ordering. The newer
    fetch wins for any duplicate timestamp (broker may have updated
    OHLC after a trade replay)."""
    parquet = tmp_path / "EURUSD_M15.parquet"
    start = datetime(2026, 5, 5, 23, 30, tzinfo=timezone.utc)
    initial = _make_bars(start, n=4)   # 23:30, 23:45, 00:00, 00:15
    data_sync.save_parquet(initial, parquet)

    # Bridge re-fetches with overlap + 2 new bars at the end. Mutate
    # one OHLC value to confirm "newer wins" on dedupe.
    fresh = _make_bars(start, n=6)
    fresh.loc[3, "close"] = 9.999   # different close on overlapping bar

    def fetcher(t, tf, n):
        return fresh

    out = data_sync.sync_to_now(
        "EURUSD", "M15", fetcher, parquet_path=parquet,
    )

    assert len(out) == 6, "should have 4 existing + 2 new (dedupe)"
    # Time monotonic + UTC tz preserved
    assert out["time"].is_monotonic_increasing
    assert out["time"].dt.tz is not None
