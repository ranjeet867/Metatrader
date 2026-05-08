"""Regression tests for DeploymentRunner._persist_closed_trade.

These pin down the production-grade fix shipped 2026-05-08 after the
forensic audit found three compounding bugs:

  1. trades.run_id has FK on runs.run_id, PRAGMA foreign_keys = ON,
     but the runner never created the parent runs row → every INSERT
     failed with FOREIGN KEY constraint failed.
  2. trade_idx was hardcoded to 0 → every close after the first per
     deployment violated PRIMARY KEY (run_id, trade_idx).
  3. The exception was caught with log.exception, so the runner
     kept ticking and the circuit breaker (which reads `trades` for
     realised PnL / daily loss / streak) became blind to actual losses.

Failure of any test below means we've regressed. Do not weaken these.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core import storage
from core.deployment_runner import DeploymentRunner


# ── Fixtures ──────────────────────────────────────────────────────


def _make_deployment(deployment_id: str = "rsi_30_70_EURUSD_M15",
                       ticker: str = "EURUSD", tf: str = "M15",
                       strategy: str = "rsi_30_70",
                       status: str = "paper"):
    return SimpleNamespace(
        deployment_id=deployment_id,
        ticker=ticker, tf=tf, strategy=strategy,
        status=status,
    )


def _make_closed(direction: str = "LONG",
                   entry_price: float = 1.1000,
                   stop_price: float = 1.0950,
                   target_price: float = 1.1100,
                   exit_price: float = 1.1080,
                   lots: float = 0.10,
                   realized_pnl: float = 80.0,
                   r_multiple: float = 1.6,
                   close_reason: str = "tp",
                   opened_at_utc: str = "2026-05-08T09:45:00+00:00",
                   closed_at_utc: str = "2026-05-08T11:30:00+00:00"):
    return SimpleNamespace(
        direction=direction,
        entry_price=entry_price, stop_price=stop_price,
        target_price=target_price, exit_price=exit_price,
        lots=lots, realized_pnl=realized_pnl,
        r_multiple=r_multiple, close_reason=close_reason,
        opened_at_utc=opened_at_utc, closed_at_utc=closed_at_utc,
    )


@pytest.fixture
def runner(tmp_path: Path):
    """Build a minimally-instantiated DeploymentRunner with an isolated
    DB and just the fields _persist_closed_trade needs.

    We deliberately bypass the normal __init__ flow (which tries to
    auto-resolve the account DB and start a poll loop) and patch the
    fields directly on a fresh instance — the persistence path only
    touches self.db_path, self._runs_seeded, self._trade_idx_by_dep.
    """
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    r = DeploymentRunner.__new__(DeploymentRunner)
    r.db_path = db
    r._runs_seeded = set()
    r._trade_idx_by_dep = {}
    return r


# ── Tests ─────────────────────────────────────────────────────────


def test_first_close_persists(runner):
    """The very first paper close must land in `trades`. Pre-fix this
    failed because no parent runs row existed and FK was enforced."""
    d = _make_deployment()
    runner._persist_closed_trade(d, _make_closed())
    with sqlite3.connect(runner.db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert n == 1, "first close did not persist"


def test_parent_runs_row_seeded(runner):
    """The parent `runs` row must be created so the FK is satisfied
    AND so circuit_breaker / dashboards that join trades→runs work."""
    d = _make_deployment()
    runner._persist_closed_trade(d, _make_closed())
    with sqlite3.connect(runner.db_path) as c:
        row = c.execute(
            "SELECT run_id, symbol, tf, strategy_name FROM runs "
            "WHERE run_id = ?", (d.deployment_id,),
        ).fetchone()
    assert row is not None, "parent runs row was not created"
    assert row[1] == "EURUSD"
    assert row[2] == "M15"
    assert row[3] == "rsi_30_70"


def test_two_closes_same_deployment_no_pk_collision(runner):
    """Pre-fix: trade_idx=0 hardcoded → second insert hit
    UNIQUE constraint failed on (run_id, trade_idx)."""
    d = _make_deployment()
    runner._persist_closed_trade(d, _make_closed(realized_pnl=80))
    runner._persist_closed_trade(d, _make_closed(realized_pnl=-50))
    with sqlite3.connect(runner.db_path) as c:
        rows = c.execute(
            "SELECT trade_idx, realized_pnl FROM trades "
            "WHERE run_id = ? ORDER BY trade_idx",
            (d.deployment_id,),
        ).fetchall()
    assert [r[0] for r in rows] == [0, 1]
    assert [r[1] for r in rows] == [80.0, -50.0]


def test_idx_hydrates_from_db_on_first_use(tmp_path: Path):
    """A restarted runner must read MAX(trade_idx)+1 so it doesn't
    collide with already-persisted rows."""
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    # Seed 3 trades from a "previous run".
    with sqlite3.connect(db) as c:
        c.execute("PRAGMA foreign_keys = OFF")    # pre-seed without parent
        c.execute(
            "INSERT INTO runs (run_id, started_at_utc, symbol, tf, "
            " strategy_name, config_json, starting_balance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("dep_x", "2026-05-01T00:00:00+00:00", "EURUSD", "M15",
             "rsi_30_70", "{}", 100000.0),
        )
        for i in range(3):
            c.execute(
                "INSERT INTO trades (run_id, trade_idx, symbol, "
                " direction, opened_at_utc, entry_price, stop_price, "
                " lots, mode, strategy, tf) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("dep_x", i, "EURUSD", "LONG",
                 "2026-05-01T00:00:00+00:00",
                 1.1, 1.095, 0.1, "paper", "rsi_30_70", "M15"),
            )
    r = DeploymentRunner.__new__(DeploymentRunner)
    r.db_path = db
    r._runs_seeded = set()
    r._trade_idx_by_dep = {}
    d = _make_deployment(deployment_id="dep_x")
    r._persist_closed_trade(d, _make_closed())
    with sqlite3.connect(db) as c:
        idxs = [row[0] for row in c.execute(
            "SELECT trade_idx FROM trades WHERE run_id = ? "
            "ORDER BY trade_idx", ("dep_x",))]
    assert idxs == [0, 1, 2, 3], (
        "new trade should land at idx=3, not collide with existing 0/1/2"
    )


def test_persist_failure_is_loud_not_silent(runner, caplog):
    """Pre-fix failures were swallowed by log.exception. Now they must
    log CRITICAL, write a `trade_persist_failed` bridge_alarm, and
    re-raise so the outer tick handler sees them."""
    d = _make_deployment()
    closed = _make_closed()
    # Simulate a CHECK constraint violation by sending direction=GARBAGE.
    closed.direction = "GARBAGE"

    with caplog.at_level("CRITICAL"):
        with pytest.raises(Exception):
            runner._persist_closed_trade(d, closed)

    # CRITICAL log present.
    assert any("TRADE PERSIST FAILED" in r.message for r in caplog.records), (
        "expected CRITICAL TRADE PERSIST FAILED log entry"
    )
    # Bridge alarm row written.
    with sqlite3.connect(runner.db_path) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM bridge_events "
            "WHERE method = 'trade_persist_failed'"
        ).fetchone()[0]
    assert n >= 1, "expected a trade_persist_failed bridge_alarm row"


def test_idempotency_key_is_set(runner):
    """We populate idempotency_key so retries can dedupe — required for
    the future order-resend safety net."""
    d = _make_deployment()
    runner._persist_closed_trade(d, _make_closed())
    with sqlite3.connect(runner.db_path) as c:
        key = c.execute(
            "SELECT idempotency_key FROM trades "
            "WHERE run_id = ? AND trade_idx = 0",
            (d.deployment_id,),
        ).fetchone()[0]
    assert key is not None and len(key) > 0


def test_two_deployments_have_independent_counters(runner):
    """Two different deployments must each start at trade_idx=0 — the
    counter is per-deployment, not global."""
    d1 = _make_deployment(deployment_id="dep_a")
    d2 = _make_deployment(deployment_id="dep_b")
    runner._persist_closed_trade(d1, _make_closed())
    runner._persist_closed_trade(d2, _make_closed())
    runner._persist_closed_trade(d1, _make_closed())
    with sqlite3.connect(runner.db_path) as c:
        rows = c.execute(
            "SELECT run_id, trade_idx FROM trades "
            "ORDER BY run_id, trade_idx",
        ).fetchall()
    assert rows == [("dep_a", 0), ("dep_a", 1), ("dep_b", 0)]


def test_circuit_breaker_can_now_see_realised_pnl(runner):
    """Integration check: after persistence, circuit_breaker.evaluate
    should see realised PnL > 0 (or whatever sign). Pre-fix it always
    saw 0 because trades was empty.

    We don't import circuit_breaker.py here — too many runtime deps —
    but we mirror its query directly to prove the data is now
    visible."""
    d = _make_deployment(status="live")
    runner._persist_closed_trade(d, _make_closed(realized_pnl=-200.0))
    runner._persist_closed_trade(d, _make_closed(realized_pnl=-300.0))
    runner._persist_closed_trade(d, _make_closed(realized_pnl=+50.0))
    with sqlite3.connect(runner.db_path) as c:
        total = c.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trades "
            "WHERE mode = 'live'"
        ).fetchone()[0]
        n_losses = c.execute(
            "SELECT COUNT(*) FROM trades WHERE realized_pnl < 0"
        ).fetchone()[0]
    assert total == pytest.approx(-450.0)
    assert n_losses == 2
