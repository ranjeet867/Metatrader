"""
test_account_statement.py — broker-truth realized/unrealized + FTMO buffers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from zoneinfo import ZoneInfo

from core.account_statement import StatementReader, StatementSummary
from core.ftmo_clock import FtmoRules
from core.mt5_account import (
    AccountInfo,
    BridgeDeal,
    BridgePosition,
    MT5AccountClient,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _bridge(*, balance=100_000, equity=100_000, positions=None, deals=None):
    bridge = MagicMock(spec=MT5AccountClient)
    bridge.account_info.return_value = AccountInfo(
        login=1, balance=balance, equity=equity,
        currency="USD", leverage=30,
    )
    bridge.positions_get.return_value = list(positions or [])
    bridge.history_deals_get.return_value = list(deals or [])
    return bridge


def _deal(profit, t_iso, position_id=1, entry=1, ticket=99) -> BridgeDeal:
    return BridgeDeal(
        ticket=ticket, order=ticket, position_id=position_id,
        time_utc=t_iso, type=0, entry=entry,
        symbol="US100.cash", volume=0.1, price=100.0,
        profit=profit, swap=0.0, commission=0.0, comment="",
    )


def _position(profit) -> BridgePosition:
    return BridgePosition(
        ticket=42, symbol="US100.cash", type=0, volume=0.1,
        price_open=100.0, sl=99.0, tp=102.0, price_current=100.5,
        profit=profit, swap=0.0, commission=0.0,
        time_open_utc="2026-05-05T12:00:00+00:00", magic=0, comment="",
    )


class TestSnapshot:
    def test_realized_today_uses_ftmo_reset_boundary(self):
        """A deal at 21:30 UTC counts toward 'today' (before 22:00 reset).
        A deal at 22:30 UTC counts toward the NEXT day."""
        # `now` = 23:00 UTC on 2026-05-05 → today's boundary = 22:00 UTC same day
        now = utc(2026, 5, 5, 23, 0)
        deals = [
            _deal(50, "2026-05-05T21:30:00+00:00"),    # before today's reset
            _deal(75, "2026-05-05T22:30:00+00:00"),    # AFTER reset = today
        ]
        sr = StatementReader(account_login=1, bridge=_bridge(deals=deals),
                              ftmo_rules=FtmoRules(),
                              account_baseline=100_000)
        s = sr.snapshot(now_utc=now)
        # Today (after 22:00) = 75
        assert s.realized_pnl_today == pytest.approx(75.0)
        # Total = 125
        assert s.realized_pnl_total == pytest.approx(125.0)
        assert s.n_trades_today == 1
        assert s.n_trades_total == 2

    def test_unrealized_pnl_sums_open_positions(self):
        sr = StatementReader(account_login=1,
                              bridge=_bridge(positions=[_position(50.0)]),
                              ftmo_rules=FtmoRules(),
                              account_baseline=100_000)
        s = sr.snapshot(now_utc=utc(2026, 5, 5, 12, 0))
        assert s.unrealized_pnl == pytest.approx(50.0)

    def test_daily_loss_remaining_when_today_is_negative(self):
        """Deals total -1500 today; cap is 5% of $100k = $5000 → remaining $3500."""
        deals = [_deal(-1500, "2026-05-06T01:00:00+00:00")]
        # `now` = 06-05 02:00 UTC → boundary = 05-05 22:00 UTC
        sr = StatementReader(account_login=1, bridge=_bridge(deals=deals),
                              ftmo_rules=FtmoRules(),
                              account_baseline=100_000)
        s = sr.snapshot(now_utc=utc(2026, 5, 6, 2, 0))
        assert s.daily_loss_remaining == pytest.approx(3500.0)

    def test_daily_loss_remaining_no_loss_full_buffer(self):
        deals = [_deal(+200, "2026-05-06T01:00:00+00:00")]
        sr = StatementReader(account_login=1, bridge=_bridge(deals=deals),
                              ftmo_rules=FtmoRules(),
                              account_baseline=100_000)
        s = sr.snapshot(now_utc=utc(2026, 5, 6, 2, 0))
        # Profitable today → daily_loss_remaining is full $5000
        assert s.daily_loss_remaining == pytest.approx(5000.0)


class TestDailyPnLCurve:
    def test_buckets_by_ftmo_reset_boundary(self):
        """Two deals on different FTMO days → two rows."""
        deals = [
            _deal(100, "2026-05-04T12:00:00+00:00"),
            _deal(50,  "2026-05-05T12:00:00+00:00"),
        ]
        sr = StatementReader(account_login=1, bridge=_bridge(deals=deals),
                              ftmo_rules=FtmoRules(),
                              account_baseline=100_000)
        df = sr.daily_pnl_curve(days=10, now_utc=utc(2026, 5, 6, 0, 0))
        assert len(df) == 2
        assert df["realized_pnl"].sum() == pytest.approx(150.0)


class TestUserTzDoesNotChangeBoundaries:
    """Even with user_tz=Asia/Kolkata, the FTMO boundary is 22:00 UTC."""

    def test_kolkata_user_still_uses_utc_boundary(self):
        rules = FtmoRules(user_tz=ZoneInfo("Asia/Kolkata"))
        # Deal at 22:30 UTC counts as next-day FTMO regardless of tz
        deals = [_deal(100, "2026-05-05T22:30:00+00:00")]
        sr = StatementReader(account_login=1, bridge=_bridge(deals=deals),
                              ftmo_rules=rules, account_baseline=100_000)
        s = sr.snapshot(now_utc=utc(2026, 5, 6, 12, 0))
        assert s.realized_pnl_today == pytest.approx(100.0)
