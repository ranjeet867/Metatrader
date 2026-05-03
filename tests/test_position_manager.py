"""
test_position_manager.py — broker-truth open positions, close one / all,
and reconcile (manual_close_in_mt5 detection + phantom safety).
"""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core import account_manager, storage
from core.journal import JournalWriter
from core.mt5_account import (
    BridgeDeal,
    BridgePosition,
    CloseOrderResult,
    MT5AccountClient,
)
from core.position_manager import (
    CloseResult,
    PositionManager,
    ReconcileReport,
)


def _tmp_db() -> Path:
    p = Path(tempfile.mkstemp(suffix=".db")[1])
    storage.init_schema(p)
    return p


def _bridge_with(positions, deals, *, close_results=None, fail_on=None):
    """Build a mock MT5AccountClient.

    positions: list[BridgePosition]
    deals: list[BridgeDeal]
    close_results: dict[ticket, CloseOrderResult] (or None for default ok)
    fail_on: set of tickets that should raise on position_close
    """
    fail_on = fail_on or set()
    close_results = close_results or {}
    bridge = MagicMock(spec=MT5AccountClient)
    bridge.positions_get.return_value = list(positions)
    bridge.history_deals_get.return_value = list(deals)

    def _close(ticket, deviation=20):
        if ticket in fail_on:
            raise RuntimeError(f"bridge fail on {ticket}")
        return close_results.get(
            ticket,
            CloseOrderResult(ok=True, retcode=10009, deal=99,
                              price=100.0, comment="ok"),
        )
    bridge.position_close.side_effect = _close
    return bridge


def _bp(ticket, symbol="US100.cash", t=0, vol=0.1, price=100.0,
         profit=0.0, magic=0, comment="") -> BridgePosition:
    return BridgePosition(
        ticket=ticket, symbol=symbol, type=t, volume=vol,
        price_open=price, sl=price - 1, tp=price + 2,
        price_current=price, profit=profit, swap=0.0, commission=0.0,
        time_open_utc="2026-05-05T12:00:00+00:00", magic=magic, comment=comment,
    )


def _bd(ticket, position_id, profit, t_iso="2026-05-05T13:00:00+00:00",
         entry=1, symbol="US100.cash") -> BridgeDeal:
    return BridgeDeal(
        ticket=ticket, order=ticket, position_id=position_id,
        time_utc=t_iso, type=0, entry=entry, symbol=symbol,
        volume=0.1, price=100.0, profit=profit, swap=0.0, commission=0.0,
        comment="",
    )


@pytest.fixture(autouse=True)
def isolated_emergency_stop(tmp_path, monkeypatch):
    """Ensure the global EMERGENCY_STOP path is per-test."""
    es_file = tmp_path / "EMERGENCY_STOP"
    monkeypatch.setattr(account_manager, "EMERGENCY_STOP_FILE", es_file)


# ---------------------------------------------------------------------------
# list_open
# ---------------------------------------------------------------------------

class TestListOpen:
    def test_returns_long_and_short(self):
        bridge = _bridge_with([_bp(1, t=0), _bp(2, t=1, symbol="EURUSD")], [])
        pm = PositionManager(account_login=1, bridge=bridge,
                              db_path=_tmp_db())
        ps = pm.list_open()
        assert len(ps) == 2
        assert ps[0].direction == "long"
        assert ps[1].direction == "short"

    def test_empty_when_broker_has_none(self):
        bridge = _bridge_with([], [])
        pm = PositionManager(account_login=1, bridge=bridge,
                              db_path=_tmp_db())
        assert pm.list_open() == []


# ---------------------------------------------------------------------------
# close_one + close_all
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_one_writes_journal_event(self):
        deals = [_bd(99, position_id=1, profit=42.0)]
        bridge = _bridge_with([_bp(1)], deals)
        db = _tmp_db()
        pm = PositionManager(account_login=1, bridge=bridge, db_path=db)
        # Pre-populate open_positions so close_one has something to drop
        pm.record_open(ticket=1, symbol="US100.cash", direction="long",
                        lots=0.1, entry_price=100.0,
                        stop_price=99.0, target_price=102.0,
                        opened_at_utc="2026-05-05T12:00:00+00:00")
        res = pm.close_one(1, reason="manual_close_ui")
        assert res.ok
        assert res.realized_pnl == pytest.approx(42.0)
        # Journal row exists
        with storage.connect(db) as c:
            row = c.execute(
                "SELECT method FROM bridge_events "
                "WHERE method LIKE 'close:%' ORDER BY ROWID DESC LIMIT 1"
            ).fetchone()
        assert row[0] == "close:manual_close_ui"

    def test_close_one_blocked_by_emergency_stop_unless_override(self):
        bridge = _bridge_with([_bp(1)], [])
        pm = PositionManager(account_login=1, bridge=bridge,
                              db_path=_tmp_db())
        account_manager.touch_emergency_stop()
        try:
            with pytest.raises(PermissionError):
                pm.close_one(1, reason="manual_close_ui")
            # Override: emergency_flatten ALLOWED
            res = pm.close_one(1, reason="emergency_flatten")
            assert res.ok
        finally:
            account_manager.clear_emergency_stop()

    def test_close_all_keeps_going_after_one_failure(self):
        """If position 2 of 3 fails, 1 and 3 still close."""
        bridge = _bridge_with(
            [_bp(1), _bp(2, symbol="X"), _bp(3, symbol="Y")],
            deals=[_bd(99, 1, 10), _bd(100, 3, 5)],
            fail_on={2},
        )
        pm = PositionManager(account_login=1, bridge=bridge,
                              db_path=_tmp_db())
        results = pm.close_all(reason="close_all_ui")
        assert len(results) == 3
        # Position 2 failed; 1 and 3 succeeded
        by_ticket = {r.ticket: r for r in results}
        assert by_ticket[1].ok
        assert not by_ticket[2].ok
        assert by_ticket[3].ok


# ---------------------------------------------------------------------------
# reconcile_with_broker
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_detects_manual_close_in_mt5(self):
        # DB says position 1 is open. Broker says nothing is open.
        # History has the closing deal for position 1.
        bridge = _bridge_with(
            [],   # broker: no open positions
            deals=[_bd(99, position_id=1, profit=37.5)],
        )
        db = _tmp_db()
        pm = PositionManager(account_login=1, bridge=bridge, db_path=db)
        pm.record_open(ticket=1, symbol="US100.cash", direction="long",
                        lots=0.1, entry_price=100.0,
                        stop_price=99.0, target_price=102.0,
                        opened_at_utc="2026-05-05T12:00:00+00:00")
        rep = pm.reconcile_with_broker()
        assert rep.n_manual_closes_recorded == 1
        # Journal entry exists with the realized pnl
        with storage.connect(db) as c:
            row = c.execute(
                "SELECT method, error FROM bridge_events "
                "WHERE method='close:manual_close_in_mt5' LIMIT 1"
            ).fetchone()
        assert row is not None
        assert "ticket=1" in row[1]
        assert "+37" in row[1]

    def test_reconcile_is_idempotent(self):
        """Second call writes no extra journal rows."""
        bridge = _bridge_with(
            [],
            deals=[_bd(99, position_id=1, profit=10)],
        )
        db = _tmp_db()
        pm = PositionManager(account_login=1, bridge=bridge, db_path=db)
        pm.record_open(ticket=1, symbol="US100.cash", direction="long",
                        lots=0.1, entry_price=100.0, stop_price=99,
                        target_price=102,
                        opened_at_utc="2026-05-05T12:00:00+00:00")
        pm.reconcile_with_broker()
        with storage.connect(db) as c:
            n1 = c.execute(
                "SELECT COUNT(*) FROM bridge_events "
                "WHERE method='close:manual_close_in_mt5'"
            ).fetchone()[0]
        # Second run on same broker state. The DB row for ticket 1 was
        # already deleted by the first reconcile, so we re-add it to
        # simulate the same starting state.
        pm.record_open(ticket=1, symbol="US100.cash", direction="long",
                        lots=0.1, entry_price=100.0, stop_price=99,
                        target_price=102,
                        opened_at_utc="2026-05-05T12:00:00+00:00")
        pm.reconcile_with_broker()
        with storage.connect(db) as c:
            n2 = c.execute(
                "SELECT COUNT(*) FROM bridge_events "
                "WHERE method='close:manual_close_in_mt5'"
            ).fetchone()[0]
        assert n1 == n2 == 1

    def test_phantom_open_logs_but_does_not_close(self):
        """Broker has position 99 we never opened. Reconcile logs but
        does NOT call position_close on it."""
        bridge = _bridge_with([_bp(99, magic=42, comment="manual")], [])
        db = _tmp_db()
        pm = PositionManager(account_login=1, bridge=bridge, db_path=db)
        rep = pm.reconcile_with_broker()
        assert rep.n_phantom == 1
        assert 99 in rep.phantom_tickets
        # CRITICAL: position_close was NOT called
        bridge.position_close.assert_not_called()
        # Journal has the warning
        with storage.connect(db) as c:
            row = c.execute(
                "SELECT method FROM bridge_events "
                "WHERE method='phantom_open_detected' LIMIT 1"
            ).fetchone()
        assert row is not None
