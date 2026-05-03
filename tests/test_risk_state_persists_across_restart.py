"""
test_risk_state_persists_across_restart.py — INVARIANT-6 explicit.

The dashboard / paper_loop can be restarted at any time. The risk-engine
state must come back identical: consecutive_losses, daily_loss_pct,
cooldown timers — all persisted to data/v2.db.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.risk_engine import LiveRiskTracker


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def _tmp_db() -> Path:
    return Path(tempfile.mkstemp(suffix=".db")[1])


def test_consecutive_losses_count_recovered_after_restart():
    db = _tmp_db()
    tr1 = LiveRiskTracker(db_path=db, max_consecutive_losses=3,
                            cooldown_minutes=60)
    for i in range(2):
        tr1.record_trade_close(symbol="US100.cash", strategy="vol_breakout",
                                r_multiple=-1.0, pnl=-100,
                                account_balance=100_000,
                                now_utc=NOW + timedelta(minutes=i))
    # Drop tr1, build a NEW tracker against the same DB
    del tr1
    tr2 = LiveRiskTracker(db_path=db, max_consecutive_losses=3,
                            cooldown_minutes=60)
    s = tr2.state_for("US100.cash", "vol_breakout")
    assert s["consecutive_losses"] == 2


def test_daily_loss_pct_recovered_after_restart():
    db = _tmp_db()
    tr1 = LiveRiskTracker(db_path=db, daily_loss_cap_pct=4.5)
    tr1.record_trade_close(symbol="US100.cash", strategy="vol_breakout",
                            r_multiple=-1.0, pnl=-2_500,
                            account_balance=100_000, now_utc=NOW)
    pct1 = tr1.account_daily_loss_pct()
    del tr1

    tr2 = LiveRiskTracker(db_path=db, daily_loss_cap_pct=4.5)
    pct2 = tr2.account_daily_loss_pct()
    assert pct1 == pct2 == 2.5


def test_cooldown_timer_recovered_after_restart():
    db = _tmp_db()
    tr1 = LiveRiskTracker(db_path=db, max_consecutive_losses=2,
                            cooldown_minutes=120)
    for i in range(2):
        tr1.record_trade_close(symbol="X", strategy="s",
                                r_multiple=-1.0, pnl=-100,
                                account_balance=100_000,
                                now_utc=NOW + timedelta(minutes=i))
    # State written to DB; cooldown_until ≈ NOW + 1 min + 120 min
    del tr1

    tr2 = LiveRiskTracker(db_path=db, max_consecutive_losses=2,
                            cooldown_minutes=120)
    d = tr2.pre_trade_check(symbol="X", strategy="s",
                              now_utc=NOW + timedelta(minutes=30))
    assert not d.allowed
    assert "cooldown" in d.reason
