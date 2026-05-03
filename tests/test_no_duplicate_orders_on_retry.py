"""
test_no_duplicate_orders_on_retry.py — bridge timeout retry MUST NOT cause
duplicate fills. The idempotency_key is the dedup mechanism: even if the
caller retries with the same key after a bridge error, the second call is
denied at the pre-flight stage.

The spec calls this test out by filename — keeping it as a separate module
makes the intent obvious to anyone reviewing safety guarantees.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        live_safety_override_parity_recency=True,
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


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def test_same_idempotency_key_within_window_denied(tmp_path):
    bridge = lambda m, p: {"ok": True, "ticket": 1}
    ex = LiveExecutor(db_path=tmp_path / "v2.db", risk_config=_build_cfg(),
                       time_guard_cfg=_build_tg(), account_login=1,
                       bridge_call=bridge, emergency_stop_dir=tmp_path)
    ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                   sl=21000.0, tp=22000.0, strategy="vol_breakout",
                   idempotency_key="RETRY_KEY", now_utc=NOW)
    # 30 seconds later, identical retry — must be denied
    with pytest.raises(SafetyCheckRejection) as exc:
        ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                       sl=21000.0, tp=22000.0, strategy="vol_breakout",
                       idempotency_key="RETRY_KEY",
                       now_utc=NOW + timedelta(seconds=30))
    assert exc.value.check_id == "idempotency"


def test_key_dedup_holds_even_when_first_call_raised(tmp_path):
    """If the first call's bridge_send raises (e.g. timeout), the key was
    ALREADY recorded. A retry with the same key must still be denied —
    the operator should generate a new key for a fresh attempt."""
    call_count = [0]

    def bridge(method, params):
        call_count[0] += 1
        # First call: raise; second call: succeed
        if call_count[0] == 1:
            raise TimeoutError("bridge timed out")
        return {"ok": True, "ticket": 1}

    ex = LiveExecutor(db_path=tmp_path / "v2.db", risk_config=_build_cfg(),
                       time_guard_cfg=_build_tg(), account_login=1,
                       bridge_call=bridge, emergency_stop_dir=tmp_path)

    with pytest.raises(TimeoutError):
        ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                       sl=21000.0, tp=22000.0, strategy="vol_breakout",
                       idempotency_key="K_TIMEOUT", now_utc=NOW)
    # Now retry with the SAME key — denied at pre-flight
    with pytest.raises(SafetyCheckRejection) as exc:
        ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                       sl=21000.0, tp=22000.0, strategy="vol_breakout",
                       idempotency_key="K_TIMEOUT",
                       now_utc=NOW + timedelta(seconds=10))
    assert exc.value.check_id == "idempotency"
    # The bridge was called only ONCE (the second attempt never reached it)
    assert call_count[0] == 1


def test_different_keys_both_succeed(tmp_path):
    bridge = lambda m, p: {"ok": True, "ticket": 1}
    ex = LiveExecutor(db_path=tmp_path / "v2.db", risk_config=_build_cfg(),
                       time_guard_cfg=_build_tg(), account_login=1,
                       bridge_call=bridge, emergency_stop_dir=tmp_path)
    o1 = ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                         sl=21000.0, tp=22000.0, strategy="vol_breakout",
                         idempotency_key="K1", now_utc=NOW)
    o2 = ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                         sl=21000.0, tp=22000.0, strategy="vol_breakout",
                         idempotency_key="K2", now_utc=NOW + timedelta(seconds=5))
    assert o1.idempotency_key == "K1"
    assert o2.idempotency_key == "K2"


def test_dedup_window_expires(tmp_path):
    """After seen_window_seconds elapse, the same key may be reused."""
    bridge = lambda m, p: {"ok": True, "ticket": 1}
    ex = LiveExecutor(db_path=tmp_path / "v2.db", risk_config=_build_cfg(),
                       time_guard_cfg=_build_tg(), account_login=1,
                       bridge_call=bridge, emergency_stop_dir=tmp_path,
                       seen_window_seconds=10.0)
    ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                   sl=21000.0, tp=22000.0, strategy="vol_breakout",
                   idempotency_key="K", now_utc=NOW)
    # 30 seconds later (> 10s window) — allowed again
    o2 = ex.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                         sl=21000.0, tp=22000.0, strategy="vol_breakout",
                         idempotency_key="K",
                         now_utc=NOW + timedelta(seconds=30))
    assert o2.ticket == 1
