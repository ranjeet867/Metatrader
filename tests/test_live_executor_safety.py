"""
test_live_executor_safety.py — INVARIANT-6 — every pre-flight gate.

The bridge is mocked; no real orders. Each test isolates one denial path.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import storage
from core.config import RiskConfig
from core.live_executor import (
    LiveExecutor,
    SafetyCheckRejection,
)
from core.parity_gate import ParityGate
from core.risk_engine import LiveRiskTracker
from core.time_guards import TimeGuardCfg


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp())


def _build_cfg(allowed_accounts=(), override_parity=False) -> RiskConfig:
    """Build a minimal RiskConfig matching DEFAULT_CONFIG values."""
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
        live_safety_allowed_accounts=tuple(allowed_accounts),
        live_safety_override_parity_recency=override_parity,
        raw={},
    )


def _build_tg_cfg() -> TimeGuardCfg:
    return TimeGuardCfg(
        weekend_flat_all=True,
        daily_close_flat_classes=("stock", "index"),
        us_session_close_hhmm="20:00",
        flat_buffer_minutes=5,
        no_entry_minutes_before_close=30,
        asset_class_overrides={"index": ["US100.cash"]},
    )


@pytest.fixture
def executor(tmp_path):
    """Fresh LiveExecutor with mocked bridge that always returns ok+ticket=42."""
    db = tmp_path / "v2.db"
    cfg = _build_cfg()
    tg = _build_tg_cfg()
    bridge_calls = []

    def bridge(method, params):
        bridge_calls.append((method, params))
        return {"ok": True, "ticket": 42}

    ex = LiveExecutor(
        db_path=db,
        risk_config=cfg,
        time_guard_cfg=tg,
        account_login=12345,
        bridge_call=bridge,
        emergency_stop_dir=tmp_path,
    )
    ex._bridge_calls = bridge_calls   # for inspection
    return ex


# A "safe" timestamp away from any close window
SAFE_NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_passes_all_gates(executor):
    # Pre-record a parity pass so check #5 succeeds
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=1))
    order = executor.send_order(
        symbol="US100.cash", direction="LONG", lots=1.0,
        sl=21000.0, tp=22000.0,
        strategy="vol_breakout",
        idempotency_key="K1",
        now_utc=SAFE_NOW,
    )
    assert order.ticket == 42
    assert executor._bridge_calls[0][0] == "order_send"


# ---------------------------------------------------------------------------
# Pre-flight check #1 — EMERGENCY_STOP
# ---------------------------------------------------------------------------

def test_emergency_stop_file_blocks(executor, tmp_path):
    (tmp_path / "EMERGENCY_STOP").touch()
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(
            symbol="US100.cash", direction="LONG", lots=1.0,
            sl=21000.0, tp=22000.0,
            strategy="vol_breakout", idempotency_key="K1",
            now_utc=SAFE_NOW,
        )
    assert exc.value.check_id == "emergency_stop"
    # No bridge call was made
    assert executor._bridge_calls == []


def test_emergency_stop_checked_on_every_call(executor, tmp_path):
    """Even if a previous call succeeded, the next call must re-check."""
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=1))
    executor.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                         sl=21000.0, tp=22000.0,
                         strategy="vol_breakout", idempotency_key="K1",
                         now_utc=SAFE_NOW)
    # Now create the file
    (tmp_path / "EMERGENCY_STOP").touch()
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1.0,
                             sl=21000.0, tp=22000.0,
                             strategy="vol_breakout", idempotency_key="K2",
                             now_utc=SAFE_NOW + timedelta(minutes=1))
    assert exc.value.check_id == "emergency_stop"


# ---------------------------------------------------------------------------
# Pre-flight check #2 — account allowlist
# ---------------------------------------------------------------------------

def test_account_not_in_allowlist_blocks(tmp_path):
    cfg = _build_cfg(allowed_accounts=[99999])     # different account
    ex = LiveExecutor(
        db_path=tmp_path / "v2.db", risk_config=cfg,
        time_guard_cfg=_build_tg_cfg(), account_login=12345,
        bridge_call=lambda m, p: {"ok": True}, emergency_stop_dir=tmp_path,
    )
    with pytest.raises(SafetyCheckRejection) as exc:
        ex.send_order(symbol="X", direction="LONG", lots=1.0, sl=99, tp=110,
                       strategy="s", idempotency_key="K", now_utc=SAFE_NOW)
    assert exc.value.check_id == "account_allowed"


def test_empty_allowlist_allows_all(executor):
    """allowed_accounts=() means no restriction."""
    executor.parity_gate.record_pass("s", 0.001, at_utc=SAFE_NOW)
    # Should not raise (empty allowlist)
    order = executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                                  sl=99, tp=110, strategy="s",
                                  idempotency_key="K", now_utc=SAFE_NOW)
    assert order.ticket == 42


# ---------------------------------------------------------------------------
# Pre-flight check #3 — daily loss cap
# ---------------------------------------------------------------------------

def test_daily_loss_cap_blocks(executor, tmp_path):
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=1))
    # Force a 5% daily loss
    executor.risk_tracker.record_trade_close(
        symbol="X", strategy="other", r_multiple=-1.0, pnl=-5_000,
        account_balance=100_000, now_utc=SAFE_NOW - timedelta(hours=1),
    )
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="vol_breakout",
                              idempotency_key="K", now_utc=SAFE_NOW)
    assert exc.value.check_id == "daily_loss_cap"


# ---------------------------------------------------------------------------
# Pre-flight check #4 — risk engine cooldown
# ---------------------------------------------------------------------------

def test_consecutive_losses_cooldown_blocks(executor):
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=1))
    # Force 3 consecutive losses (matches max_consecutive_losses default)
    for i in range(3):
        executor.risk_tracker.record_trade_close(
            symbol="US100.cash", strategy="vol_breakout",
            r_multiple=-1.0, pnl=-100,
            account_balance=100_000,
            now_utc=SAFE_NOW - timedelta(minutes=10 - i),
        )
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="vol_breakout",
                              idempotency_key="K", now_utc=SAFE_NOW)
    assert exc.value.check_id == "risk_engine"


# ---------------------------------------------------------------------------
# Pre-flight check #5 — parity recency
# ---------------------------------------------------------------------------

def test_no_recent_parity_blocks(executor):
    # Don't record any parity pass at all
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="vol_breakout",
                              idempotency_key="K", now_utc=SAFE_NOW)
    assert exc.value.check_id == "parity_recent"


def test_old_parity_blocks(executor):
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=48))
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="vol_breakout",
                              idempotency_key="K", now_utc=SAFE_NOW)
    assert exc.value.check_id == "parity_recent"


def test_parity_override_skips_recency_check(tmp_path):
    cfg = _build_cfg(override_parity=True)
    ex = LiveExecutor(
        db_path=tmp_path / "v2.db", risk_config=cfg,
        time_guard_cfg=_build_tg_cfg(), account_login=12345,
        bridge_call=lambda m, p: {"ok": True, "ticket": 1},
        emergency_stop_dir=tmp_path,
    )
    # No parity recorded; override should skip the check
    order = ex.send_order(symbol="US100.cash", direction="LONG", lots=1,
                            sl=99, tp=110, strategy="vol_breakout",
                            idempotency_key="K", now_utc=SAFE_NOW)
    assert order.ticket == 1


# ---------------------------------------------------------------------------
# Pre-flight check #6 — idempotency dedup
# ---------------------------------------------------------------------------

def test_no_duplicate_orders_on_retry(executor):
    """The CRITICAL anti-duplicate-fill test."""
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=1))
    executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                          sl=99, tp=110, strategy="vol_breakout",
                          idempotency_key="DUPKEY", now_utc=SAFE_NOW)
    # Same key, slightly later — must be denied
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="vol_breakout",
                              idempotency_key="DUPKEY",
                              now_utc=SAFE_NOW + timedelta(seconds=2))
    assert exc.value.check_id == "idempotency"


# ---------------------------------------------------------------------------
# Pre-flight check #7 — no-entry window
# ---------------------------------------------------------------------------

def test_no_entry_window_blocks_open(executor):
    executor.parity_gate.record_pass("vol_breakout", 0.001,
                                       at_utc=SAFE_NOW - timedelta(hours=1))
    # Tuesday 19:35 UTC is inside [19:30, 20:00] no-entry window
    in_window = datetime(2026, 5, 5, 19, 35, tzinfo=timezone.utc)
    with pytest.raises(SafetyCheckRejection) as exc:
        executor.send_order(symbol="US100.cash", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="vol_breakout",
                              idempotency_key="K", now_utc=in_window)
    assert exc.value.check_id == "no_entry_window"


def test_close_does_not_check_no_entry_window(executor, tmp_path):
    """Closes must work inside the no-entry window — that's how forced-flats
    reach the broker."""
    in_window = datetime(2026, 5, 5, 19, 35, tzinfo=timezone.utc)
    res = executor.close_position(ticket=42, comment="weekend_flat",
                                    now_utc=in_window)
    assert res.ticket == 42


# ---------------------------------------------------------------------------
# Audit trail — refusals are logged
# ---------------------------------------------------------------------------

def test_refusal_is_logged_to_bridge_events(executor, tmp_path):
    with pytest.raises(SafetyCheckRejection):
        executor.send_order(symbol="X", direction="LONG", lots=1,
                              sl=99, tp=110, strategy="ema_cross",
                              idempotency_key="K", now_utc=SAFE_NOW)
    with storage.connect(executor.db_path) as c:
        row = c.execute(
            "SELECT method, ok, error FROM bridge_events ORDER BY ROWID DESC LIMIT 1"
        ).fetchone()
    assert row[0].startswith("preflight_deny:")
    assert row[1] == 0
    assert "K" in row[2]
