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


def test_close_uses_position_close_method_name(executor, tmp_path):
    """REGRESSION: live_executor previously called the bridge with method
    name 'order_close' but the MT5 EA registers 'position_close'. Every
    HALT / Demote / forced-flat silently failed at the bridge.
    """
    res = executor.close_position(ticket=42, comment="manual",
                                    now_utc=SAFE_NOW)
    assert res.ticket == 42
    # The mock bridge appends every call to _bridge_calls — the most
    # recent must be position_close, not order_close.
    last_call_method, last_call_params = executor._bridge_calls[-1]
    assert last_call_method == "position_close", (
        f"expected method 'position_close' but live_executor called "
        f"'{last_call_method}' — the EA does not register that name"
    )
    assert last_call_params["ticket"] == 42
    assert last_call_params["comment"] == "manual"


def test_close_retries_on_transient_failure(tmp_path):
    """If the bridge returns {ok: False, error: ...} on the first
    attempt, the executor retries. After all retries exhausted it
    raises BridgeCloseFailed so the caller can mark the deployment
    halted and surface the error in the UI."""
    from core.live_executor import BridgeCloseFailed, LiveExecutor

    db = tmp_path / "v2.db"
    cfg = _build_cfg()
    tg = _build_tg_cfg()
    attempts = []

    def flaky_bridge(method, params):
        attempts.append((method, params))
        # Fail twice, then succeed
        if len(attempts) < 3:
            return {"ok": False, "error": "TRADE_RETCODE_REQUOTE"}
        return {"ok": True, "ticket": 42}

    ex = LiveExecutor(
        db_path=db, risk_config=cfg, time_guard_cfg=tg,
        account_login=12345, bridge_call=flaky_bridge,
        emergency_stop_dir=tmp_path,
    )
    res = ex.close_position(ticket=42, comment="t", retries=3,
                              now_utc=SAFE_NOW)
    assert res.ticket == 42
    assert len(attempts) == 3      # 2 failures + 1 success
    assert all(a[0] == "position_close" for a in attempts)


def test_close_raises_bridge_close_failed_after_retries(tmp_path):
    from core.live_executor import BridgeCloseFailed, LiveExecutor

    db = tmp_path / "v2.db"
    cfg = _build_cfg()
    tg = _build_tg_cfg()

    def always_fail(method, params):
        return {"ok": False, "error": "MARKET_CLOSED"}

    ex = LiveExecutor(
        db_path=db, risk_config=cfg, time_guard_cfg=tg,
        account_login=12345, bridge_call=always_fail,
        emergency_stop_dir=tmp_path,
    )
    with pytest.raises(BridgeCloseFailed) as exc:
        ex.close_position(ticket=99, comment="halt", retries=2,
                            now_utc=SAFE_NOW)
    assert "MARKET_CLOSED" in str(exc.value)


# ---------------------------------------------------------------------------
# REGRESSION: order_send silent-failure bug (Phase 30)
# ---------------------------------------------------------------------------

def test_order_send_raises_on_broker_rejection(tmp_path):
    """Bridge returns ok=False, retcode=10018 (market closed). Previously
    this silently returned a LiveOrder with ticket=0 and the runner
    thought the order succeeded. Now it must raise BridgeOrderRejected
    with the retcode + error preserved."""
    from core.live_executor import BridgeOrderRejected, LiveExecutor

    db = tmp_path / "v2.db"

    def rejecting_bridge(method, params):
        return {
            "ok": False,
            "retcode": 10018,
            "error": "MARKET_CLOSED",
            "comment": "Market is closed",
        }

    ex = LiveExecutor(
        db_path=db, risk_config=_build_cfg(),
        time_guard_cfg=_build_tg_cfg(),
        account_login=12345, bridge_call=rejecting_bridge,
        emergency_stop_dir=tmp_path,
    )
    ex.parity_gate.record_pass("vol_breakout", 0.0,
                                  at_utc=SAFE_NOW - timedelta(hours=1))
    with pytest.raises(BridgeOrderRejected) as exc:
        ex.send_order(symbol="US100.cash", direction="LONG",
                        lots=0.1, sl=99.0, tp=110.0,
                        strategy="vol_breakout", idempotency_key="K1",
                        now_utc=SAFE_NOW)
    assert exc.value.retcode == 10018
    assert "MARKET_CLOSED" in str(exc.value)


def test_order_send_raises_on_missing_ok_key(tmp_path):
    """Malformed bridge response (no 'ok' key). Previously defaulted to
    optimistic ok=True. Now treated as failure."""
    from core.live_executor import BridgeOrderRejected, LiveExecutor

    db = tmp_path / "v2.db"

    def malformed_bridge(method, params):
        # Note: NO 'ok' key
        return {"retcode": 10004, "ticket": 12345}

    ex = LiveExecutor(
        db_path=db, risk_config=_build_cfg(),
        time_guard_cfg=_build_tg_cfg(),
        account_login=12345, bridge_call=malformed_bridge,
        emergency_stop_dir=tmp_path,
    )
    ex.parity_gate.record_pass("vol_breakout", 0.0,
                                  at_utc=SAFE_NOW - timedelta(hours=1))
    with pytest.raises(BridgeOrderRejected):
        ex.send_order(symbol="US100.cash", direction="LONG",
                        lots=0.1, sl=99.0, tp=110.0,
                        strategy="vol_breakout", idempotency_key="K2",
                        now_utc=SAFE_NOW)


def test_order_send_raises_when_ok_true_but_ticket_zero(tmp_path):
    """If the bridge says ok=True but ticket=0, that's a contract
    violation — refuse and raise so the runner doesn't think a phantom
    order succeeded."""
    from core.live_executor import BridgeOrderRejected, LiveExecutor

    db = tmp_path / "v2.db"

    def liar_bridge(method, params):
        return {"ok": True, "ticket": 0}

    ex = LiveExecutor(
        db_path=db, risk_config=_build_cfg(),
        time_guard_cfg=_build_tg_cfg(),
        account_login=12345, bridge_call=liar_bridge,
        emergency_stop_dir=tmp_path,
    )
    ex.parity_gate.record_pass("vol_breakout", 0.0,
                                  at_utc=SAFE_NOW - timedelta(hours=1))
    with pytest.raises(BridgeOrderRejected) as exc:
        ex.send_order(symbol="US100.cash", direction="LONG",
                        lots=0.1, sl=99.0, tp=110.0,
                        strategy="vol_breakout", idempotency_key="K3",
                        now_utc=SAFE_NOW)
    assert "ticket=0" in str(exc.value)


def test_order_send_succeeds_when_ok_true_with_valid_ticket(tmp_path):
    """Happy path: the existing test fixture covers this, but pin it
    here too so we know our stricter checks didn't break the success
    path."""
    from core.live_executor import LiveExecutor

    db = tmp_path / "v2.db"

    def good_bridge(method, params):
        return {"ok": True, "ticket": 42, "retcode": 10009,
                "fill_price": 25234.7}

    ex = LiveExecutor(
        db_path=db, risk_config=_build_cfg(),
        time_guard_cfg=_build_tg_cfg(),
        account_login=12345, bridge_call=good_bridge,
        emergency_stop_dir=tmp_path,
    )
    ex.parity_gate.record_pass("vol_breakout", 0.0,
                                  at_utc=SAFE_NOW - timedelta(hours=1))
    order = ex.send_order(symbol="US100.cash", direction="LONG",
                            lots=0.1, sl=99.0, tp=110.0,
                            strategy="vol_breakout", idempotency_key="K4",
                            now_utc=SAFE_NOW)
    assert order.ticket == 42


# ---------------------------------------------------------------------------
# Audit trail — refusals are logged
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pre-flight check #8 — FTMO pre-close window
# ---------------------------------------------------------------------------

def test_ftmo_pre_close_blocks_indices_but_not_fx(tmp_path):
    """At 21:55 UTC (pre-close window), index entries are refused; FX is fine."""
    from core.ftmo_clock import FtmoClock, FtmoRules
    cfg = _build_cfg()
    bridge = lambda m, p: {"ok": True, "ticket": 1}
    ex = LiveExecutor(
        db_path=tmp_path / "v2.db", risk_config=cfg,
        time_guard_cfg=_build_tg_cfg(), account_login=1,
        bridge_call=bridge, emergency_stop_dir=tmp_path,
        ftmo_clock=FtmoClock(FtmoRules()),
    )
    # parity bypass
    ex.parity_gate.record_pass("vol_breakout", 0.001,
                                 at_utc=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc))

    pre_close = datetime(2026, 5, 5, 21, 56, tzinfo=timezone.utc)
    # Index → blocked
    with pytest.raises(SafetyCheckRejection) as exc:
        ex.send_order(symbol="US100.cash", direction="LONG", lots=1,
                       sl=99, tp=110, strategy="vol_breakout",
                       idempotency_key="K1", now_utc=pre_close)
    assert exc.value.check_id == "pre_ftmo_close"
    # FX → allowed (no_entry_window may also fire here; check that the
    # FAILURE if any is NOT pre_ftmo_close)
    try:
        ex.send_order(symbol="EURUSD", direction="LONG", lots=1,
                       sl=1.10, tp=1.15, strategy="vol_breakout",
                       idempotency_key="K2", now_utc=pre_close)
    except SafetyCheckRejection as e:
        assert e.check_id != "pre_ftmo_close"


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
