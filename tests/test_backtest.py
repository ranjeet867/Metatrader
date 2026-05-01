"""
test_backtest.py — verify the backtester against KNOWN-OUTPUT scenarios.

Every test asserts an EXACT expected outcome. If reconciliation fails on any
test, the backtester is wrong and no result above it can be trusted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.backtest import run_backtest
from core.strategy import Signal
from tests.fixtures.synthetic import (
    constant, hand_crafted_trade, linear_ramp, sawtooth, step_function,
)


# ===========================================================================
# Scenario 1 — Single LONG win at exact +2R
# ===========================================================================
class TestSingleTradeExactOutcome:
    """For a hand-crafted candle series with KNOWN target, we can compute the
    exact PnL. Verify backtester math matches."""

    def test_long_win_at_2R_exact_pnl(self):
        # Setup: enter at 100, stop at 99 (1 unit risk), target at 102 (+2R)
        entry, stop, target = 100.0, 99.0, 102.0
        candles = hand_crafted_trade(
            entry_bar=5, entry_price=entry,
            stop_price=stop, target_price=target,
            outcome="hit_target", n_bars=30,
        )
        sig = Signal(bar_idx=5, direction="LONG",
                      entry_price=entry, stop_price=stop, target_price=target,
                      reason="test")
        result = run_backtest(
            candles, [sig],
            starting_balance=100_000, lots=0.1, money_per_unit_price=1.0,
        )

        # Expected PnL = (target - entry) × lots × $/unit = 2 × 0.1 × 1.0 = $0.20
        assert result.n_trades == 1
        t = result.trades[0]
        assert abs(t.realized_pnl - 0.20) < 1e-9, \
            f"expected pnl=0.20, got {t.realized_pnl}"
        assert abs(t.r_multiple - 2.0) < 1e-9
        assert t.close_reason == "target"
        # Reconciliation MUST hold
        assert result.reconciles, \
            f"sum_pnl={result.sum_realized_pnl} eq_pnl={result.equity_curve_pnl}"

    def test_long_loss_at_neg1R_exact_pnl(self):
        entry, stop, target = 100.0, 99.0, 102.0
        candles = hand_crafted_trade(
            entry_bar=5, entry_price=entry,
            stop_price=stop, target_price=target,
            outcome="hit_stop", n_bars=30,
        )
        sig = Signal(bar_idx=5, direction="LONG",
                      entry_price=entry, stop_price=stop, target_price=target)
        result = run_backtest(
            candles, [sig],
            starting_balance=100_000, lots=0.1, money_per_unit_price=1.0,
        )
        assert result.n_trades == 1
        t = result.trades[0]
        # Expected PnL = (stop - entry) × 0.1 × 1.0 = -1 × 0.1 = -$0.10
        assert abs(t.realized_pnl - (-0.10)) < 1e-9
        assert abs(t.r_multiple - (-1.0)) < 1e-9
        assert t.close_reason == "stop"
        assert result.reconciles


# ===========================================================================
# Scenario 2 — RECONCILIATION INVARIANT (the v1 bug catch)
# ===========================================================================
class TestReconciliationInvariant:
    """sum(closed_pnl) MUST equal equity_curve_delta. Always.

    This test is the gate. If it fails for ANY scenario, the backtester
    has a bug (v1 had this exact failure)."""

    def test_reconciles_on_no_trades(self):
        candles = constant(price=100, n_bars=50)
        result = run_backtest(candles, signals=[],
                               starting_balance=100_000, lots=0.1,
                               money_per_unit_price=1.0)
        assert result.n_trades == 0
        assert result.reconciles

    def test_reconciles_on_single_winning_trade(self):
        candles = hand_crafted_trade(
            entry_bar=3, entry_price=100, stop_price=99, target_price=102,
            outcome="hit_target", n_bars=20,
        )
        sig = Signal(bar_idx=3, direction="LONG",
                      entry_price=100, stop_price=99, target_price=102)
        result = run_backtest(candles, [sig],
                               starting_balance=100_000, lots=0.5,
                               money_per_unit_price=1.0)
        assert result.reconciles

    def test_reconciles_on_single_losing_trade(self):
        candles = hand_crafted_trade(
            entry_bar=3, entry_price=100, stop_price=99, target_price=102,
            outcome="hit_stop", n_bars=20,
        )
        sig = Signal(bar_idx=3, direction="LONG",
                      entry_price=100, stop_price=99, target_price=102)
        result = run_backtest(candles, [sig],
                               starting_balance=100_000, lots=0.5,
                               money_per_unit_price=1.0)
        assert result.reconciles

    def test_reconciles_on_many_random_seeds(self):
        """Property test: across many random seeds with sawtooth data and
        deterministic signals, reconciliation must hold every time."""
        rng = np.random.default_rng(42)
        for seed in range(50):
            # Build a sawtooth that guarantees both wins and losses
            df = sawtooth(low_price=100, high_price=110,
                           up_bars=10, down_bars=10, n_cycles=8)
            # Plant random signals: every Kth bar with a random K, alternating direction
            k = int(rng.integers(7, 25))
            signals = []
            for i in range(k, len(df) - 10, k):
                direction = "LONG" if (i // k) % 2 == 0 else "SHORT"
                entry = float(df["close"].iloc[i])
                if direction == "LONG":
                    stop = entry - 1.0
                    target = entry + 2.0
                else:
                    stop = entry + 1.0
                    target = entry - 2.0
                signals.append(Signal(bar_idx=i, direction=direction,
                                        entry_price=entry, stop_price=stop,
                                        target_price=target))
            result = run_backtest(df, signals, starting_balance=100_000,
                                    lots=0.1, money_per_unit_price=1.0)
            assert result.reconciles, \
                f"seed {seed} reconciliation FAILED: " \
                f"sum_pnl={result.sum_realized_pnl:.4f} " \
                f"eq_pnl={result.equity_curve_pnl:.4f} " \
                f"diff={result.sum_realized_pnl - result.equity_curve_pnl:.4f}"


# ===========================================================================
# Scenario 3 — Idempotency: same input twice → identical output
# ===========================================================================
class TestIdempotency:
    def test_same_run_twice_identical_results(self):
        candles = step_function(low_price=100, high_price=110,
                                  bars_at_low=50, bars_at_high=50)
        sig = Signal(bar_idx=50, direction="LONG",
                      entry_price=110, stop_price=109, target_price=112)
        a = run_backtest(candles, [sig], starting_balance=100_000,
                          lots=0.1, money_per_unit_price=1.0)
        b = run_backtest(candles, [sig], starting_balance=100_000,
                          lots=0.1, money_per_unit_price=1.0)
        assert a.n_trades == b.n_trades
        assert a.sum_realized_pnl == b.sum_realized_pnl
        assert a.ending_balance == b.ending_balance
        # Equity curves must be element-wise identical
        np.testing.assert_array_equal(a.equity_curve["equity"].values,
                                       b.equity_curve["equity"].values)


# ===========================================================================
# Scenario 4 — Single-position guarantee: signals during open position ignored
# ===========================================================================
class TestSinglePositionConstraint:
    def test_second_signal_ignored_while_position_open(self):
        # Build candles: bar 5 enters LONG, never hits SL or TP, eventually closes at end
        # Bar 10 has another signal that should be IGNORED.
        candles = constant(price=100, n_bars=30)
        signals = [
            Signal(bar_idx=5, direction="LONG", entry_price=100, stop_price=99,
                   target_price=200),  # target unreachable on flat data
            Signal(bar_idx=10, direction="LONG", entry_price=100, stop_price=99,
                   target_price=200),
        ]
        result = run_backtest(candles, signals, starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0)
        assert result.n_trades == 1
        assert result.trades[0].close_reason == "end_of_data"


# ===========================================================================
# Scenario 5 — Geometry validation
# ===========================================================================
class TestSignalValidation:
    def test_long_with_stop_above_entry_rejected(self):
        candles = constant(price=100, n_bars=20)
        bad = Signal(bar_idx=5, direction="LONG", entry_price=100,
                      stop_price=101, target_price=102)
        with pytest.raises(ValueError):
            run_backtest(candles, [bad], starting_balance=100_000, lots=0.1,
                         money_per_unit_price=1.0)

    def test_short_with_stop_below_entry_rejected(self):
        candles = constant(price=100, n_bars=20)
        bad = Signal(bar_idx=5, direction="SHORT", entry_price=100,
                      stop_price=99, target_price=98)
        with pytest.raises(ValueError):
            run_backtest(candles, [bad], starting_balance=100_000, lots=0.1,
                         money_per_unit_price=1.0)
