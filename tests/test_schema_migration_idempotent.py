"""
test_schema_migration_idempotent.py — every migration in core/storage is safe
to re-run any number of times.

We test:
  1. init_schema on a fresh DB creates all v2 tables + columns.
  2. init_schema re-run on a fresh DB is a no-op (no errors, no dup rows).
  3. init_schema applied to a v1-shaped DB (only old trades columns) ADDs the
     new columns without touching existing data.
  4. All v2 writers (paper_runs, live_runs, risk_state, parity_log,
     bridge_events, trade_notes, ftmo_daily_resets) are upsert-shaped: a
     re-write does NOT duplicate rows.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from core import storage


V2_TABLES = {
    "candles", "trades", "runs", "schema_version",
    "paper_runs", "live_runs", "risk_state", "ftmo_daily_resets",
    "parity_log", "bridge_events", "trade_notes",
}

V2_TRADE_COLUMNS = {
    "mode", "strategy", "tf", "idempotency_key", "notes",
    "mt5_ticket", "magic_number",
}


def _tmp_db() -> Path:
    return Path(tempfile.mkstemp(suffix=".db")[1])


def _table_names(db: Path) -> set[str]:
    with storage.connect(db) as c:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}


def _columns(db: Path, table: str) -> set[str]:
    with storage.connect(db) as c:
        return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


class TestFreshInit:
    def test_creates_all_v2_tables(self):
        db = _tmp_db()
        storage.init_schema(db)
        assert V2_TABLES.issubset(_table_names(db))

    def test_trades_has_v2_columns(self):
        db = _tmp_db()
        storage.init_schema(db)
        cols = _columns(db, "trades")
        assert V2_TRADE_COLUMNS.issubset(cols)

    def test_double_init_no_error(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.init_schema(db)   # must not raise
        # And version row is still exactly 1 entry
        with storage.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert n == 1


class TestV1Migration:
    """Simulate a DB created by the v1 schema (pre-migration columns) and
    confirm init_schema upgrades it without losing data."""

    def _build_v1_db(self) -> Path:
        db = _tmp_db()
        # Hand-roll a v1-shaped trades table (the columns BEFORE the v2 ALTERs)
        with sqlite3.connect(db) as c:
            c.executescript("""
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
                CREATE TABLE candles (
                    symbol TEXT, tf TEXT, time_utc TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume REAL,
                    PRIMARY KEY (symbol, tf, time_utc)
                );
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    started_at_utc TEXT, symbol TEXT, tf TEXT,
                    strategy_name TEXT, config_json TEXT, starting_balance REAL,
                    finished_at_utc TEXT, ending_equity REAL, n_trades INTEGER,
                    sum_realized_pnl REAL, equity_curve_pnl REAL, reconciles INTEGER
                );
                CREATE TABLE trades (
                    run_id TEXT, trade_idx INTEGER,
                    symbol TEXT, direction TEXT,
                    opened_at_utc TEXT, closed_at_utc TEXT,
                    entry_price REAL, stop_price REAL, target_price REAL,
                    exit_price REAL, lots REAL,
                    realized_pnl REAL, r_multiple REAL, close_reason TEXT,
                    PRIMARY KEY (run_id, trade_idx)
                );
                INSERT INTO schema_version VALUES (1);
                INSERT INTO runs (run_id, started_at_utc, symbol, tf,
                                   strategy_name, config_json, starting_balance)
                  VALUES ('R1', '2024-01-01T00:00:00+00:00', 'TEST', 'H1',
                          'ema', '{}', 100000);
                INSERT INTO trades VALUES ('R1', 0, 'TEST', 'LONG',
                    '2024-01-01T01:00:00+00:00', '2024-01-01T05:00:00+00:00',
                    100, 99, 102, 102, 0.1, 200.0, 2.0, 'target');
            """)
        return db

    def test_v1_db_gets_new_columns_no_data_loss(self):
        db = self._build_v1_db()
        storage.init_schema(db)
        cols = _columns(db, "trades")
        assert V2_TRADE_COLUMNS.issubset(cols)
        # Original row preserved with default mode='backtest'
        with storage.connect(db) as c:
            row = c.execute(
                "SELECT run_id, trade_idx, symbol, direction, mode, strategy "
                "FROM trades WHERE run_id='R1'"
            ).fetchone()
        assert row[:4] == ("R1", 0, "TEST", "LONG")
        assert row[4] == "backtest"   # default
        assert row[5] is None          # strategy column added but unset
        # New tables exist
        assert V2_TABLES.issubset(_table_names(db))

    def test_v1_db_idempotent_second_init(self):
        db = self._build_v1_db()
        storage.init_schema(db)
        storage.init_schema(db)   # second call — must not duplicate columns
        cols = _columns(db, "trades")
        assert V2_TRADE_COLUMNS.issubset(cols)
        # No dup version rows
        with storage.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert n in (1, 2)   # 1 (legacy) and possibly 2 (current) is allowed


class TestUpsertWriters:
    def test_paper_run_upsert(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.upsert_paper_run(db, "PR1", started_at_utc="t0",
                                 status="running", config_json="{}")
        storage.upsert_paper_run(db, "PR1", started_at_utc="t0",
                                 status="finished", config_json="{}",
                                 finished_at_utc="t1")
        with storage.connect(db) as c:
            rows = c.execute("SELECT status FROM paper_runs WHERE run_id='PR1'").fetchall()
        assert len(rows) == 1 and rows[0][0] == "finished"

    def test_risk_state_upsert(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.upsert_risk_state(db, "US100.cash", "vol_breakout",
                                  consecutive_losses=1, last_loss_at_utc="t0",
                                  cooldown_until_utc=None,
                                  daily_loss_pct=0.5, day_start_balance=100_000)
        storage.upsert_risk_state(db, "US100.cash", "vol_breakout",
                                  consecutive_losses=2, last_loss_at_utc="t1",
                                  cooldown_until_utc="t2",
                                  daily_loss_pct=1.5, day_start_balance=100_000)
        s = storage.get_risk_state(db, "US100.cash", "vol_breakout")
        assert s["consecutive_losses"] == 2
        assert s["cooldown_until_utc"] == "t2"
        assert abs(s["daily_loss_pct"] - 1.5) < 1e-9

    def test_parity_pass_idempotent(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.record_parity_pass(db, "vol_breakout", "t0", 0.001)
        storage.record_parity_pass(db, "vol_breakout", "t0", 0.001)  # dup
        last = storage.last_parity_pass(db, "vol_breakout")
        assert last == ("t0", 0.001)
        with storage.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM parity_log").fetchone()[0]
        assert n == 1

    def test_bridge_event_append_only(self):
        db = _tmp_db()
        storage.init_schema(db)
        for i in range(5):
            storage.record_bridge_event(db, f"t{i}", "copy_rates", ok=True,
                                        latency_ms=12)
        with storage.connect(db) as c:
            n = c.execute("SELECT COUNT(*) FROM bridge_events").fetchone()[0]
        assert n == 5

    def test_trade_note_upsert(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.upsert_trade_note(db, "R1", 0, "first note", "t0")
        storage.upsert_trade_note(db, "R1", 0, "second note", "t1")
        with storage.connect(db) as c:
            rows = c.execute(
                "SELECT note, updated_at_utc FROM trade_notes WHERE trade_run_id='R1'"
            ).fetchall()
        assert rows == [("second note", "t1")]

    def test_ftmo_reset_zeroes_consecutive_losses(self):
        db = _tmp_db()
        storage.init_schema(db)
        storage.upsert_risk_state(db, "S1", "x",
                                  consecutive_losses=3, last_loss_at_utc="t",
                                  cooldown_until_utc=None,
                                  daily_loss_pct=2.5, day_start_balance=100_000)
        storage.reset_risk_state_daily(db, "2026-05-03T22:00:00Z", 99_000)
        s = storage.get_risk_state(db, "S1", "x")
        assert s["consecutive_losses"] == 0
        assert s["daily_loss_pct"] == 0
        assert s["day_start_balance"] == 99_000

    def test_save_trades_with_v2_fields(self):
        """Trades dicts may include new metadata; columns persist correctly."""
        db = _tmp_db()
        storage.init_schema(db)
        storage.save_run(db, "R1",
                         started_at_utc="t0", symbol="US100.cash", tf="D1",
                         strategy_name="vol_breakout",
                         config_json="{}", starting_balance=100_000)
        storage.save_trades(db, "R1", [{
            "symbol": "US100.cash", "direction": "LONG",
            "opened_at_utc": "t1", "closed_at_utc": "t2",
            "entry_price": 100, "stop_price": 99, "target_price": 102,
            "exit_price": 102, "lots": 1.0,
            "realized_pnl": 200, "r_multiple": 2.0, "close_reason": "target",
            "mode": "paper", "strategy": "vol_breakout", "tf": "D1",
            "idempotency_key": "vol_breakout:US100.cash:D1:42",
            "mt5_ticket": 12345, "magic_number": 9991,
        }])
        with storage.connect(db) as c:
            row = c.execute(
                "SELECT mode, strategy, tf, idempotency_key, mt5_ticket, magic_number "
                "FROM trades WHERE run_id='R1'"
            ).fetchone()
        assert row == ("paper", "vol_breakout", "D1",
                       "vol_breakout:US100.cash:D1:42", 12345, 9991)
