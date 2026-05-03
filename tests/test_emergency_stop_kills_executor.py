"""
test_emergency_stop_kills_executor.py — INVARIANT: the emergency-stop file
WORKS even if the dashboard is broken.

Touch the file, call live_executor.send_order, expect rejection with
check_id='emergency_stop'. The file path is checked freshly on every send
(not cached), so the operator can drop the file at any moment from any
process and the next order is refused.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.config import RiskConfig
from core.live_executor import LiveExecutor, SafetyCheckRejection
from core.time_guards import TimeGuardCfg


def _build_cfg() -> RiskConfig:
    return RiskConfig(
        weekend_flat_all=True,
        daily_close_flat_classes=("stock", "index"),
        us_session_close_utc="20:00",
        flat_buffer_minutes=5,
        no_entry_minutes_before_close=30,
        daily_loss_cap_pct=4.5,
        max_consecutive_losses=3,
        max_open_positions=3,
        ftmo_daily_reset_utc="22:00",
        asset_class_overrides={"index": ("US100.cash",)},
        live_safety_allowed_accounts=(),
        live_safety_override_parity_recency=True,   # bypass parity for THIS test
        raw={},
    )


def _build_tg() -> TimeGuardCfg:
    return TimeGuardCfg(
        weekend_flat_all=True,
        daily_close_flat_classes=("stock", "index"),
        us_session_close_hhmm="20:00",
        flat_buffer_minutes=5,
        no_entry_minutes_before_close=30,
        asset_class_overrides={"index": ["US100.cash"]},
    )


SAFE_NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def test_emergency_stop_blocks_send_order(tmp_path):
    """The simplest, blunt-force version of the safety test the spec called out."""
    db = tmp_path / "v2.db"

    def fake_bridge(method, params):
        return {"ok": True, "ticket": 1}

    ex = LiveExecutor(
        db_path=db, risk_config=_build_cfg(),
        time_guard_cfg=_build_tg(), account_login=1,
        bridge_call=fake_bridge, emergency_stop_dir=tmp_path,
    )
    # Touch the file
    (tmp_path / "EMERGENCY_STOP").touch()
    with pytest.raises(SafetyCheckRejection) as exc:
        ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                       sl=21000.0, tp=22000.0, strategy="vol_breakout",
                       idempotency_key="K", now_utc=SAFE_NOW)
    assert exc.value.check_id == "emergency_stop"


def test_emergency_stop_blocks_close_position(tmp_path):
    db = tmp_path / "v2.db"
    bridge = lambda m, p: {"ok": True, "ticket": 1}
    ex = LiveExecutor(db_path=db, risk_config=_build_cfg(),
                       time_guard_cfg=_build_tg(), account_login=1,
                       bridge_call=bridge, emergency_stop_dir=tmp_path)
    (tmp_path / "EMERGENCY_STOP").touch()
    with pytest.raises(SafetyCheckRejection) as exc:
        ex.close_position(ticket=42, comment="manual")
    assert exc.value.check_id == "emergency_stop"


def test_emergency_stop_removed_allows_orders(tmp_path):
    """If the operator REMOVES the file, the next call succeeds."""
    db = tmp_path / "v2.db"
    bridge = lambda m, p: {"ok": True, "ticket": 1}
    ex = LiveExecutor(db_path=db, risk_config=_build_cfg(),
                       time_guard_cfg=_build_tg(), account_login=1,
                       bridge_call=bridge, emergency_stop_dir=tmp_path)
    sentinel = tmp_path / "EMERGENCY_STOP"
    sentinel.touch()
    with pytest.raises(SafetyCheckRejection):
        ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                       sl=21000.0, tp=22000.0, strategy="vol_breakout",
                       idempotency_key="K1", now_utc=SAFE_NOW)
    # Remove file
    sentinel.unlink()
    order = ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                            sl=21000.0, tp=22000.0, strategy="vol_breakout",
                            idempotency_key="K2", now_utc=SAFE_NOW)
    assert order.ticket == 1
