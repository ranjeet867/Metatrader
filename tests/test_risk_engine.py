"""
test_risk_engine.py — INVARIANT-6: persistent per-strategy risk gates.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.risk_engine import LiveRiskTracker, PositionSizer


def _tmp_db() -> Path:
    return Path(tempfile.mkstemp(suffix=".db")[1])


# ---------------------------------------------------------------------------
# PositionSizer
# ---------------------------------------------------------------------------

class TestPositionSizer:
    def test_basic_sizing(self):
        # $100k × 1% = $1000 risk; stop_distance=10, money_per_unit=$1
        # raw lots = 1000 / 10 = 100 lots
        lots = PositionSizer.lots_for_risk(
            account_balance=100_000, risk_pct=1.0,
            stop_distance=10.0, money_per_unit=1.0,
            volume_step=0.1,
        )
        assert lots == pytest.approx(100.0)

    def test_rounds_down_to_volume_step(self):
        # $1000 risk, stop=$3, money_per_unit=$1 → raw=333.33 lots,
        # round down to 0.1 → 333.3
        lots = PositionSizer.lots_for_risk(
            account_balance=100_000, risk_pct=1.0,
            stop_distance=3.0, money_per_unit=1.0,
            volume_step=0.1, volume_min=0.1,
        )
        assert lots == pytest.approx(333.3)

    def test_below_volume_min_returns_zero(self):
        # raw lots = 100 / (1000 × 1) = 0.1; volume_min=1.0 → too small
        lots = PositionSizer.lots_for_risk(
            account_balance=10_000, risk_pct=1.0,
            stop_distance=1000.0, money_per_unit=1.0,
            volume_step=1.0, volume_min=1.0,
        )
        assert lots == 0.0

    def test_zero_balance_returns_zero(self):
        assert PositionSizer.lots_for_risk(0, 1.0, 10, 1.0) == 0.0

    def test_zero_stop_distance_returns_zero(self):
        assert PositionSizer.lots_for_risk(100_000, 1.0, 0, 1.0) == 0.0

    def test_invalid_volume_step_raises(self):
        with pytest.raises(ValueError):
            PositionSizer.lots_for_risk(100_000, 1.0, 10, 1.0, volume_step=0)


# ---------------------------------------------------------------------------
# LiveRiskTracker
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def _tracker(**kwargs) -> LiveRiskTracker:
    return LiveRiskTracker(db_path=_tmp_db(),
                            max_consecutive_losses=2,
                            cooldown_minutes=60,
                            daily_loss_cap_pct=4.5,
                            **kwargs)


class TestConsecutiveLosses:
    def test_first_loss_no_deny(self):
        tr = _tracker()
        tr.record_trade_close(symbol="X", strategy="s",
                              r_multiple=-1.0, pnl=-100.0,
                              account_balance=100_000, now_utc=NOW)
        assert tr.pre_trade_check(symbol="X", strategy="s",
                                   now_utc=NOW + timedelta(minutes=1)).allowed

    def test_two_consecutive_losses_denies(self):
        tr = _tracker()
        for i in range(2):
            tr.record_trade_close(symbol="X", strategy="s",
                                  r_multiple=-1.0, pnl=-100.0,
                                  account_balance=100_000,
                                  now_utc=NOW + timedelta(minutes=i))
        # Now at the cap (max=2) — cooldown set
        d = tr.pre_trade_check(symbol="X", strategy="s",
                                now_utc=NOW + timedelta(minutes=10))
        assert not d.allowed
        assert "cooldown" in d.reason

    def test_winning_trade_resets_streak(self):
        tr = _tracker()
        tr.record_trade_close(symbol="X", strategy="s",
                              r_multiple=-1.0, pnl=-100.0,
                              account_balance=100_000, now_utc=NOW)
        tr.record_trade_close(symbol="X", strategy="s",
                              r_multiple=2.0, pnl=200.0,
                              account_balance=100_000,
                              now_utc=NOW + timedelta(minutes=1))
        s = tr.state_for("X", "s")
        assert s["consecutive_losses"] == 0


class TestCooldown:
    def test_cooldown_expires(self):
        tr = _tracker()
        for i in range(2):
            tr.record_trade_close(symbol="X", strategy="s",
                                  r_multiple=-1.0, pnl=-100.0,
                                  account_balance=100_000,
                                  now_utc=NOW + timedelta(minutes=i))
        # Inside cooldown
        assert not tr.pre_trade_check(symbol="X", strategy="s",
                                       now_utc=NOW + timedelta(minutes=30)).allowed
        # After cooldown
        d = tr.pre_trade_check(symbol="X", strategy="s",
                                now_utc=NOW + timedelta(hours=2))
        assert d.allowed


class TestDailyLossCap:
    def test_daily_loss_cap_blocks(self):
        tr = _tracker()
        # One $5000 loss = 5% on $100k → exceeds 4.5% cap
        tr.record_trade_close(symbol="X", strategy="s",
                              r_multiple=-1.0, pnl=-5_000.0,
                              account_balance=100_000, now_utc=NOW)
        d = tr.pre_trade_check(symbol="Y", strategy="other",
                                now_utc=NOW + timedelta(minutes=10))
        assert not d.allowed
        assert "daily_loss_pct" in d.reason

    def test_daily_reset_clears_loss_pct(self):
        tr = _tracker()
        tr.record_trade_close(symbol="X", strategy="s",
                              r_multiple=-1.0, pnl=-5_000.0,
                              account_balance=100_000, now_utc=NOW)
        tr.reset_daily(at_utc=NOW + timedelta(hours=10),
                       day_start_balance=95_000)
        s = tr.state_for("X", "s")
        assert s["daily_loss_pct"] == 0
        assert s["consecutive_losses"] == 0


class TestPersistence:
    """INVARIANT-6: state survives a process restart."""

    def test_state_persists_across_restart(self):
        db = _tmp_db()
        tr1 = LiveRiskTracker(db_path=db, max_consecutive_losses=2,
                                cooldown_minutes=60)
        tr1.record_trade_close(symbol="X", strategy="s",
                                r_multiple=-1.0, pnl=-100,
                                account_balance=100_000, now_utc=NOW)
        tr1.record_trade_close(symbol="X", strategy="s",
                                r_multiple=-1.0, pnl=-100,
                                account_balance=100_000,
                                now_utc=NOW + timedelta(minutes=1))
        # New process, same DB
        tr2 = LiveRiskTracker(db_path=db, max_consecutive_losses=2,
                                cooldown_minutes=60)
        s = tr2.state_for("X", "s")
        assert s["consecutive_losses"] == 2
        assert not tr2.pre_trade_check(symbol="X", strategy="s",
                                         now_utc=NOW + timedelta(minutes=10)).allowed
