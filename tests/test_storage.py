"""
test_storage.py — verify SQLite schema enforces invariants and is idempotent.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from core import storage
from tests.fixtures.synthetic import constant, linear_ramp


def _tmp_db():
    return Path(tempfile.mkstemp(suffix=".db")[1])


class TestSchemaInit:
    def test_init_creates_tables(self):
        db = _tmp_db()
        storage.init_schema(db)
        with storage.connect(db) as c:
            tables = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        assert "candles" in tables
        assert "trades" in tables
        assert "runs" in tables
        assert "schema_version" in tables

    def test_init_is_idempotent(self):
        """Calling init_schema twice doesn't fail or duplicate version."""
        db = _tmp_db()
        storage.init_schema(db)
        storage.init_schema(db)   # should not raise
        with storage.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert n == 1


class TestCandlePersistence:
    def test_save_and_load_round_trip(self):
        db = _tmp_db()
        storage.init_schema(db)
        df = linear_ramp(start_price=100, step=0.5, n_bars=50)
        n = storage.save_candles(db, "TEST", "H1", df)
        assert n == 50

        loaded = storage.load_candles(db, "TEST", "H1")
        assert len(loaded) == 50
        # Closes must round-trip exactly (within float tolerance)
        for i in range(50):
            assert abs(loaded["close"].iloc[i] - df["close"].iloc[i]) < 1e-9

    def test_save_is_idempotent(self):
        """Saving the same candles twice does NOT duplicate or corrupt."""
        db = _tmp_db()
        storage.init_schema(db)
        df = linear_ramp(n_bars=20)
        storage.save_candles(db, "TEST", "H1", df)
        n = storage.save_candles(db, "TEST", "H1", df)
        # INSERT OR IGNORE → second save returns 0 inserts
        assert n == 0
        loaded = storage.load_candles(db, "TEST", "H1")
        assert len(loaded) == 20

    def test_invalid_candle_rejected_negative_price(self):
        """validate_candles raises ValueError on negative prices."""
        db = _tmp_db()
        storage.init_schema(db)
        bad = pd.DataFrame({
            "time": [pd.Timestamp("2024-01-01", tz="UTC")],
            "open": [100.0], "high": [-5.0], "low": [99.0], "close": [100.0],
            "volume": [1.0],
        })
        with pytest.raises(ValueError):
            storage.save_candles(db, "TEST", "H1", bad)

    def test_invalid_candle_rejected_high_below_low(self):
        """validate_candles raises ValueError on high < low."""
        db = _tmp_db()
        storage.init_schema(db)
        bad = pd.DataFrame({
            "time": [pd.Timestamp("2024-01-01", tz="UTC")],
            "open": [100.0], "high": [99.0], "low": [101.0], "close": [100.0],
            "volume": [1.0],
        })
        with pytest.raises(ValueError):
            storage.save_candles(db, "TEST", "H1", bad)

    def test_validate_catches_duplicate_timestamps(self):
        """Duplicate times (would corrupt indicators) get rejected."""
        ts = pd.Timestamp("2024-01-01", tz="UTC")
        bad = pd.DataFrame({
            "time": [ts, ts],
            "open": [100.0, 100.0], "high": [101.0, 101.0],
            "low": [99.0, 99.0], "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        })
        with pytest.raises(ValueError):
            storage.validate_candles(bad)

    def test_validate_catches_unsorted_time(self):
        """Out-of-order timestamps get rejected."""
        bad = pd.DataFrame({
            "time": [pd.Timestamp("2024-01-02", tz="UTC"),
                     pd.Timestamp("2024-01-01", tz="UTC")],
            "open": [100.0, 100.0], "high": [101.0, 101.0],
            "low": [99.0, 99.0], "close": [100.0, 100.0],
            "volume": [1.0, 1.0],
        })
        with pytest.raises(ValueError):
            storage.validate_candles(bad)

    def test_load_returns_sorted_when_inserts_per_chunk(self):
        """If we save chunks (each ascending), load returns globally ascending."""
        db = _tmp_db()
        storage.init_schema(db)
        df = linear_ramp(n_bars=20)
        # Save in two chunks (each individually time-sorted)
        storage.save_candles(db, "TEST", "H1", df.iloc[10:].reset_index(drop=True))
        storage.save_candles(db, "TEST", "H1", df.iloc[:10].reset_index(drop=True))
        loaded = storage.load_candles(db, "TEST", "H1")
        for i in range(1, len(loaded)):
            assert loaded["time"].iloc[i] > loaded["time"].iloc[i - 1]


class TestRunsAndTrades:
    def test_save_run_and_trades(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.save_run(
            db, "RUN_001",
            started_at_utc="2024-01-01T00:00:00+00:00",
            symbol="TEST", tf="H1", strategy_name="ema_cross",
            config_json='{"foo": 1}', starting_balance=100_000,
        )
        trades = [
            {"symbol": "TEST", "direction": "LONG",
             "opened_at_utc": "2024-01-01T01:00:00+00:00",
             "closed_at_utc": "2024-01-01T05:00:00+00:00",
             "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0,
             "exit_price": 102.0, "lots": 0.1,
             "realized_pnl": 200.0, "r_multiple": 2.0, "close_reason": "tp"},
        ]
        n = storage.save_trades(db, "RUN_001", trades)
        assert n == 1

    def test_trade_save_is_idempotent_via_replace(self):
        """Same (run_id, trade_idx) → REPLACE, no duplicate."""
        db = _tmp_db()
        storage.init_schema(db)
        storage.save_run(db, "R1", started_at_utc="2024-01-01T00:00:00+00:00",
                         symbol="TEST", tf="H1", strategy_name="x",
                         config_json="{}", starting_balance=100_000)
        t = [{"symbol": "TEST", "direction": "LONG",
              "opened_at_utc": "2024-01-01T01:00:00+00:00",
              "entry_price": 100, "stop_price": 99, "lots": 0.1}]
        storage.save_trades(db, "R1", t)
        storage.save_trades(db, "R1", t)
        with storage.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM trades WHERE run_id='R1'").fetchone()[0]
        assert n == 1, "REPLACE should keep exactly 1 row, not duplicate"

    def test_run_not_duplicable(self):
        """Two runs with the same run_id → second one fails."""
        db = _tmp_db()
        storage.init_schema(db)
        kwargs = dict(started_at_utc="2024-01-01T00:00:00+00:00", symbol="TEST",
                      tf="H1", strategy_name="x", config_json="{}",
                      starting_balance=100_000)
        storage.save_run(db, "DUP", **kwargs)
        with pytest.raises(sqlite3.IntegrityError):
            storage.save_run(db, "DUP", **kwargs)
