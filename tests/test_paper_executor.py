"""
test_paper_executor.py — INVARIANT-3 (single PnL calculator) + INVARIANT-8.

Verifies:
  - Idempotency-key collision rejection
  - Single-symbol-position rule
  - max_open_positions cap
  - Bad signal geometry rejected
  - mark_to_market = 0 immediately after open at fill price (no slip)
  - Close-after-target produces identical trade tuple as run_backtest
  - update_bar applies forced-flats: weekend / daily / FX-exempt
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from core.backtest import run_backtest
from core.paper_executor import (
    Bar,
    BadSignalGeometry,
    IdempotencyCollision,
    MaxOpenPositionsExceeded,
    PaperExecutor,
    SymbolAlreadyOpen,
)
from core.strategy import Signal
from core.time_guards import TimeGuardCfg


CFG = TimeGuardCfg(
    weekend_flat_all=True,
    daily_close_flat_classes=("stock", "index"),
    us_session_close_hhmm="20:00",
    flat_buffer_minutes=5,
    no_entry_minutes_before_close=30,
    asset_class_overrides={
        "stock":  ["META"],
        "index":  ["US100.cash"],
        "metal":  ["XAUUSD"],
        "energy": [],
        "fx":     [],
    },
)


def _open_default(executor: PaperExecutor, *, symbol="US100.cash",
                   key="K1", direction="LONG"):
    return executor.open(
        symbol=symbol, direction=direction,
        signal_entry_price=100.0, stop_price=99.0, target_price=102.0,
        lots=1.0, money_per_unit_price=1.0,
        idempotency_key=key,
        opened_at_bar_idx=0, opened_at_utc="2026-05-04T12:00:00+00:00",
        atr_at_signal_bar=0.0,
    )


class TestIdempotency:
    def test_same_key_twice_rejected(self):
        ex = PaperExecutor()
        _open_default(ex, key="DUP", symbol="A.idx")
        with pytest.raises(IdempotencyCollision):
            _open_default(ex, key="DUP", symbol="B.idx")

    def test_different_keys_allowed(self):
        ex = PaperExecutor(max_open_positions=5)
        _open_default(ex, key="K1", symbol="A.idx")
        _open_default(ex, key="K2", symbol="B.idx")
        assert ex.n_open == 2


class TestSinglePositionPerSymbol:
    def test_second_open_on_same_symbol_rejected(self):
        ex = PaperExecutor(max_open_positions=5)
        _open_default(ex, key="K1", symbol="A.idx")
        with pytest.raises(SymbolAlreadyOpen):
            _open_default(ex, key="K2", symbol="A.idx")


class TestMaxOpen:
    def test_max_open_cap_enforced(self):
        ex = PaperExecutor(max_open_positions=2)
        _open_default(ex, key="K1", symbol="A.idx")
        _open_default(ex, key="K2", symbol="B.idx")
        with pytest.raises(MaxOpenPositionsExceeded):
            _open_default(ex, key="K3", symbol="C.idx")


class TestBadGeometry:
    def test_long_with_stop_above_entry_rejected(self):
        ex = PaperExecutor()
        with pytest.raises(BadSignalGeometry):
            ex.open(symbol="X", direction="LONG",
                    signal_entry_price=100.0, stop_price=101.0,
                    target_price=105.0,
                    lots=1.0, money_per_unit_price=1.0,
                    idempotency_key="K", opened_at_bar_idx=0,
                    opened_at_utc="t")

    def test_short_with_stop_below_entry_rejected(self):
        ex = PaperExecutor()
        with pytest.raises(BadSignalGeometry):
            ex.open(symbol="X", direction="SHORT",
                    signal_entry_price=100.0, stop_price=99.0,
                    target_price=95.0,
                    lots=1.0, money_per_unit_price=1.0,
                    idempotency_key="K", opened_at_bar_idx=0,
                    opened_at_utc="t")

    def test_zero_lots_rejected(self):
        ex = PaperExecutor()
        with pytest.raises(BadSignalGeometry):
            ex.open(symbol="X", direction="LONG",
                    signal_entry_price=100.0, stop_price=99.0,
                    target_price=102.0,
                    lots=0.0, money_per_unit_price=1.0,
                    idempotency_key="K", opened_at_bar_idx=0,
                    opened_at_utc="t")


class TestMarkToMarket:
    def test_mtm_at_entry_is_zero_no_slip(self):
        ex = PaperExecutor()    # zero slippage
        _open_default(ex)
        # Mark at the actual fill price — floating PnL must be exactly 0
        pos = ex.get_position("US100.cash")
        assert ex.mark_to_market("US100.cash", pos.actual_entry_price) == 0.0

    def test_mtm_long_above_entry_positive(self):
        ex = PaperExecutor()
        _open_default(ex)
        # +$2.00 move on 1 lot, money_per_unit=$1 → +$2.00
        assert ex.mark_to_market("US100.cash", 102.0) == pytest.approx(2.0)

    def test_mtm_short_below_entry_positive(self):
        ex = PaperExecutor()
        ex.open(symbol="X", direction="SHORT",
                signal_entry_price=100.0, stop_price=101.0, target_price=98.0,
                lots=1.0, money_per_unit_price=1.0,
                idempotency_key="K", opened_at_bar_idx=0,
                opened_at_utc="t", atr_at_signal_bar=0.0)
        assert ex.mark_to_market("X", 99.0) == pytest.approx(1.0)


class TestPnLParityWithBacktest:
    """The fundamental INVARIANT-3 test: a target-hit trade through PaperExecutor
    must produce numerically identical realized_pnl to run_backtest."""

    def test_long_target_hit_matches_backtest_pnl(self):
        # Build a 5-bar candle series where bar 1 fires a LONG signal that
        # hits the target at bar 3. Use linear ramp prices.
        times = pd.date_range("2026-05-04T12:00:00Z", periods=5, freq="h", tz="UTC")
        df = pd.DataFrame({
            "time": times,
            "open":  [100, 101, 102, 103, 104.0],
            "high":  [101, 102, 103, 104, 105.0],
            "low":   [99,  100, 101, 102, 103.0],
            "close": [100, 101, 102.5, 103, 104.0],
            "volume": [1000.0]*5,
        })
        sig = Signal(bar_idx=1, direction="LONG",
                     entry_price=101.0, stop_price=100.0, target_price=102.5)

        # Backtest run
        bt = run_backtest(df, [sig], starting_balance=10_000, lots=1.0,
                          money_per_unit_price=1.0, commission_per_trade=2.0)
        assert bt.n_trades == 1

        # Paper executor — open at bar 1's close (price 101.0), let target hit
        ex = PaperExecutor(commission_per_trade=2.0,
                           slippage_per_fill_atr_frac=0.0)
        ex.open(symbol="X", direction="LONG",
                signal_entry_price=101.0, stop_price=100.0, target_price=102.5,
                lots=1.0, money_per_unit_price=1.0,
                idempotency_key="K", opened_at_bar_idx=1,
                opened_at_utc=str(times[1]),
                atr_at_signal_bar=0.0)
        # Drive bars 2 then 3 — target hits on bar 2 (high=103 ≥ target 102.5)
        bar2 = Bar(idx=2, high=103.0, low=101.0, close=102.5,
                   time_utc=times[2].to_pydatetime(), atr_value=0.0)
        closed = ex.update_bar("X", bar2)
        assert closed is not None
        # Numerical equality
        assert closed.realized_pnl == pytest.approx(bt.trades[0].realized_pnl)
        assert closed.exit_price == pytest.approx(bt.trades[0].exit_price)
        assert closed.close_reason == "target"


class TestForcedFlatsViaUpdateBar:
    """update_bar must close on weekend / daily flat windows."""

    def test_weekend_flat_closes_long_at_friday_close(self):
        ex = PaperExecutor(time_guard_cfg=CFG)
        _open_default(ex, symbol="EURUSD")     # FX, exempt from daily_flat
        # Bar at Fri 2026-05-08 19:00 UTC; bar_close_utc 20:00 → in close window
        bar_open_dt = datetime(2026, 5, 8, 19, 0, tzinfo=timezone.utc)
        bar = Bar(idx=5, high=100.5, low=99.5, close=100.2,
                   time_utc=bar_open_dt, atr_value=0.0)
        # bar_close_utc = bar_open + 1h = Fri 20:00 — in [19:55, 20:00]
        bar_close = datetime(2026, 5, 8, 20, 0, tzinfo=timezone.utc)
        closed = ex.update_bar("EURUSD", bar, bar_close_utc=bar_close)
        assert closed is not None
        assert closed.close_reason == "weekend_flat"

    def test_daily_flat_closes_index_at_weekday_close(self):
        ex = PaperExecutor(time_guard_cfg=CFG)
        _open_default(ex, symbol="US100.cash")    # index
        bar_open_dt = datetime(2026, 5, 5, 19, 0, tzinfo=timezone.utc)  # Tue
        bar = Bar(idx=5, high=100.5, low=99.5, close=100.2,
                   time_utc=bar_open_dt, atr_value=0.0)
        bar_close = datetime(2026, 5, 5, 20, 0, tzinfo=timezone.utc)
        closed = ex.update_bar("US100.cash", bar, bar_close_utc=bar_close)
        assert closed is not None
        assert closed.close_reason == "daily_close_flat"

    def test_daily_flat_does_not_close_fx(self):
        ex = PaperExecutor(time_guard_cfg=CFG)
        _open_default(ex, symbol="EURUSD")
        bar_open_dt = datetime(2026, 5, 5, 19, 0, tzinfo=timezone.utc)  # Tue
        bar = Bar(idx=5, high=100.5, low=99.5, close=100.2,
                   time_utc=bar_open_dt, atr_value=0.0)
        bar_close = datetime(2026, 5, 5, 20, 0, tzinfo=timezone.utc)
        closed = ex.update_bar("EURUSD", bar, bar_close_utc=bar_close)
        assert closed is None     # FX not in daily_close_flat_classes
        assert ex.has_position("EURUSD")

    def test_no_time_guard_cfg_no_force_flat(self):
        ex = PaperExecutor(time_guard_cfg=None)   # no guards configured
        _open_default(ex, symbol="US100.cash")
        bar_open_dt = datetime(2026, 5, 5, 19, 0, tzinfo=timezone.utc)
        bar = Bar(idx=5, high=100.5, low=99.5, close=100.2,
                   time_utc=bar_open_dt, atr_value=0.0)
        bar_close = datetime(2026, 5, 5, 20, 0, tzinfo=timezone.utc)
        closed = ex.update_bar("US100.cash", bar, bar_close_utc=bar_close)
        assert closed is None
        assert ex.has_position("US100.cash")


class TestExplicitClose:
    def test_close_records_trade_with_reason(self):
        ex = PaperExecutor()
        _open_default(ex)
        closed = ex.close("US100.cash", 101.0, reason="manual_stop")
        assert closed is not None
        assert closed.close_reason == "manual_stop"
        assert closed.exit_price == 101.0
        assert not ex.has_position("US100.cash")

    def test_close_unknown_symbol_returns_none(self):
        ex = PaperExecutor()
        assert ex.close("NOPE", 100.0, reason="x") is None


class TestClosedTradesAccumulate:
    def test_closed_trades_appear_in_property(self):
        ex = PaperExecutor(max_open_positions=5)
        _open_default(ex, key="K1", symbol="A")
        _open_default(ex, key="K2", symbol="B")
        ex.close("A", 105.0, reason="manual")
        ex.close("B", 99.0, reason="manual")
        assert len(ex.closed_trades) == 2
