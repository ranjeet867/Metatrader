"""
test_journal.py — append-only audit trail.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from core import storage
from core.journal import JournalWriter


def _tmp_db() -> Path:
    return Path(tempfile.mkstemp(suffix=".db")[1])


def test_record_bridge_event_appends_only():
    j = JournalWriter(_tmp_db())
    for i in range(3):
        j.record_bridge_event(method="copy_rates", latency_ms=12 + i, ok=True)
    with storage.connect(j.db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM bridge_events").fetchone()[0]
    assert n == 3


def test_record_forced_flat_idempotent_on_same_timestamp():
    j = JournalWriter(_tmp_db())
    ts = datetime(2026, 5, 8, 19, 55, tzinfo=timezone.utc)
    j.record_forced_flat(symbol="US100.cash", mode="paper",
                         reason="weekend_flat", mark_price=21000.0,
                         pnl_at_close=147.20, occurred_at_utc=ts)
    j.record_forced_flat(symbol="US100.cash", mode="paper",
                         reason="weekend_flat", mark_price=21000.0,
                         pnl_at_close=147.20, occurred_at_utc=ts)
    with storage.connect(j.db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM forced_flat_events").fetchone()[0]
    assert n == 1


def test_record_trade_persists():
    j = JournalWriter(_tmp_db())
    storage.save_run(j.db_path, "R1",
                      started_at_utc="2026-05-04T00:00:00Z",
                      symbol="US100.cash", tf="D1",
                      strategy_name="vol_breakout",
                      config_json="{}", starting_balance=100_000)
    j.record_trade({
        "symbol": "US100.cash", "direction": "LONG",
        "opened_at_utc": "2026-05-04T01:00:00Z",
        "closed_at_utc": "2026-05-04T05:00:00Z",
        "entry_price": 100, "stop_price": 99, "target_price": 102,
        "exit_price": 102, "lots": 1.0,
        "realized_pnl": 200, "r_multiple": 2.0, "close_reason": "target",
        "mode": "paper", "strategy": "vol_breakout", "tf": "D1",
        "idempotency_key": "K1",
    }, run_id="R1")
    with storage.connect(j.db_path) as c:
        rows = c.execute("SELECT close_reason FROM trades WHERE run_id='R1'").fetchall()
    assert rows == [("target",)]


def test_upsert_paper_run_then_finished():
    j = JournalWriter(_tmp_db())
    j.upsert_paper_run("PR1", status="running", config_json="{}")
    j.upsert_paper_run("PR1", status="finished", config_json="{}")
    with storage.connect(j.db_path) as c:
        rows = c.execute("SELECT status FROM paper_runs WHERE run_id='PR1'").fetchall()
    assert len(rows) == 1 and rows[0][0] == "finished"


def test_bridge_event_with_error_flagged():
    j = JournalWriter(_tmp_db())
    j.record_bridge_event(method="copy_rates", latency_ms=5000, ok=False,
                           error="bridge timeout")
    with storage.connect(j.db_path) as c:
        row = c.execute(
            "SELECT ok, error FROM bridge_events ORDER BY pinged_at_utc DESC LIMIT 1"
        ).fetchone()
    assert row[0] == 0 and row[1] == "bridge timeout"
