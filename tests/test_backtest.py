"""
test_backtest.py — verify the backtester against KNOWN-OUTPUT scenarios.

Every test asserts an EXACT expected outcome. If reconciliation fails on any
test, the backtester is wrong and no result above it can be trusted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.backtest import partition_train_test, run_backtest, summarize_trades
from core.indicators import atr_wilder
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

    def test_reconciles_with_commission(self):
        """Commission deducted per-trade must NOT break reconciliation."""
        candles = hand_crafted_trade(
            entry_bar=3, entry_price=100, stop_price=99, target_price=102,
            outcome="hit_target", n_bars=20,
        )
        sig = Signal(bar_idx=3, direction="LONG",
                      entry_price=100, stop_price=99, target_price=102)
        result = run_backtest(candles, [sig],
                                starting_balance=100_000, lots=0.5,
                                money_per_unit_price=1.0,
                                commission_per_trade=3.0)
        # Win = 0.5 × $2 = $1.00; minus $3 commission = -$2.00
        assert abs(result.trades[0].realized_pnl - (-2.0)) < 1e-9
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
            # Half the seeds include commission to test that path too
            comm = 2.0 if seed % 2 == 0 else 0.0
            result = run_backtest(df, signals, starting_balance=100_000,
                                    lots=0.1, money_per_unit_price=1.0,
                                    commission_per_trade=comm)
            assert result.reconciles, \
                f"seed {seed} reconciliation FAILED (comm=${comm}): " \
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


# ===========================================================================
# Scenario 6 — Slippage modeling
# ===========================================================================
def _ranged_trade_candles(entry_bar: int, entry_price: float,
                           stop_price: float, target_price: float,
                           n_bars: int, range_per_bar: float) -> pd.DataFrame:
    """Like hand_crafted_trade(hit_target) but every bar has H-L = range_per_bar
    with H = close + range/2, L = close - range/2. This makes ATR well-defined
    and predictable so the slippage math is verifiable.

    Bars 0..entry_bar-1: close = entry_price.
    Bars entry_bar..n_bars-1: close ramps linearly toward target_price.
    No SL bar will be hit (stop is always below the lowest low).
    """
    if not stop_price < entry_price < target_price:
        raise ValueError("expects LONG geometry")
    times = pd.date_range("2024-01-01", periods=n_bars, freq="h", tz="UTC")
    closes = np.full(n_bars, entry_price, dtype=float)
    bars_to_ramp = n_bars - entry_bar - 1
    if bars_to_ramp > 0:
        ramp = np.linspace(entry_price, target_price, bars_to_ramp + 1)
        closes[entry_bar:] = ramp[: n_bars - entry_bar]
    half = range_per_bar / 2.0
    highs = closes + half
    lows = closes - half
    # Ensure stop is always BELOW the minimum low so SL never fires accidentally
    if stop_price >= float(lows.min()):
        raise ValueError(
            f"stop {stop_price} would be hit by min(low)={float(lows.min())}"
        )
    # Ensure target IS reached on the last bar (close = target, high = target+half)
    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": np.full(n_bars, 1000, dtype=float),
    })


class TestSlippage:
    """Slippage shifts entry price up (LONG) and exit price down (LONG), against
    the trade. Reconciliation must hold and PnL math must match the formula."""

    def test_zero_slippage_matches_no_slippage(self):
        """Default slippage=0 must produce identical results to before."""
        candles = hand_crafted_trade(
            entry_bar=5, entry_price=100, stop_price=99, target_price=102,
            outcome="hit_target", n_bars=30,
        )
        sig = Signal(bar_idx=5, direction="LONG",
                      entry_price=100, stop_price=99, target_price=102)
        no_slip = run_backtest(candles, [sig], starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0)
        with_slip = run_backtest(candles, [sig], starting_balance=100_000,
                                  lots=0.1, money_per_unit_price=1.0,
                                  slippage_per_fill_atr_frac=0.0)
        assert no_slip.sum_realized_pnl == with_slip.sum_realized_pnl
        assert no_slip.reconciles and with_slip.reconciles

    def test_long_slippage_exact_pnl(self):
        """For a LONG trade hitting TP, with slip frac s and ATR a at fill bars,
        PnL = (target - s_exit - entry - s_entry) × lots × $/unit - commission."""
        candles = _ranged_trade_candles(
            entry_bar=5, entry_price=100.0, stop_price=95.0, target_price=102.0,
            n_bars=30, range_per_bar=0.5,
        )
        sig = Signal(bar_idx=5, direction="LONG",
                      entry_price=100.0, stop_price=95.0, target_price=102.0)
        slip_frac = 0.2
        result = run_backtest(
            candles, [sig], starting_balance=100_000, lots=0.1,
            money_per_unit_price=1.0,
            slippage_per_fill_atr_frac=slip_frac,
        )
        assert result.n_trades == 1
        t = result.trades[0]
        assert t.close_reason == "target"
        # Compute expected ATR at the entry bar and the exit bar from the same
        # source the backtester uses.
        atr = atr_wilder(candles, 14).to_numpy()
        expected_entry_slip = atr[t.entry_bar_idx] * slip_frac
        expected_exit_slip = atr[t.exit_bar_idx] * slip_frac
        expected_actual_entry = 100.0 + expected_entry_slip
        expected_actual_exit = 102.0 - expected_exit_slip
        expected_pnl = (expected_actual_exit - expected_actual_entry) * 0.1 * 1.0
        assert abs(t.entry_slip - expected_entry_slip) < 1e-9
        assert abs(t.exit_slip - expected_exit_slip) < 1e-9
        assert abs(t.entry_price - expected_actual_entry) < 1e-9
        assert abs(t.exit_price - expected_actual_exit) < 1e-9
        assert abs(t.realized_pnl - expected_pnl) < 1e-9
        assert t.signal_entry_price == 100.0
        assert result.reconciles

    def test_short_slippage_exact_pnl(self):
        """SHORT mirror: entry slips DOWN against the trade, exit slips UP."""
        # Build a ranged "hit_target for SHORT": price ramps from 100 down to 98
        n_bars = 30
        entry_bar = 5
        entry, stop, target = 100.0, 105.0, 98.0
        times = pd.date_range("2024-01-01", periods=n_bars, freq="h", tz="UTC")
        closes = np.full(n_bars, entry, dtype=float)
        ramp = np.linspace(entry, target, n_bars - entry_bar)
        closes[entry_bar:] = ramp
        half = 0.25
        highs = closes + half
        lows = closes - half
        # Ensure SL not hit
        assert stop > float(highs.max())
        candles = pd.DataFrame({
            "time": times, "open": closes, "high": highs, "low": lows,
            "close": closes, "volume": np.full(n_bars, 1000.0),
        })
        sig = Signal(bar_idx=entry_bar, direction="SHORT",
                      entry_price=entry, stop_price=stop, target_price=target)
        slip_frac = 0.15
        result = run_backtest(
            candles, [sig], starting_balance=100_000, lots=0.1,
            money_per_unit_price=1.0,
            slippage_per_fill_atr_frac=slip_frac,
        )
        assert result.n_trades == 1
        t = result.trades[0]
        assert t.close_reason == "target"
        atr = atr_wilder(candles, 14).to_numpy()
        expected_entry_slip = atr[t.entry_bar_idx] * slip_frac
        expected_exit_slip = atr[t.exit_bar_idx] * slip_frac
        # SHORT: entry slips DOWN (against), exit slips UP (against)
        expected_actual_entry = entry - expected_entry_slip
        expected_actual_exit = target + expected_exit_slip
        # SHORT PnL: (entry - exit) × lots × $/unit
        expected_pnl = (expected_actual_entry - expected_actual_exit) * 0.1 * 1.0
        assert abs(t.realized_pnl - expected_pnl) < 1e-9
        assert result.reconciles

    def test_slippage_reconciles_across_random_seeds(self):
        """Property: slippage at any frac must preserve reconciliation."""
        rng = np.random.default_rng(7)
        for seed in range(25):
            df = sawtooth(low_price=100, high_price=110,
                           up_bars=10, down_bars=10, n_cycles=8)
            k = int(rng.integers(7, 25))
            signals = []
            for i in range(k, len(df) - 10, k):
                direction = "LONG" if (i // k) % 2 == 0 else "SHORT"
                entry = float(df["close"].iloc[i])
                if direction == "LONG":
                    stop, target = entry - 1.0, entry + 2.0
                else:
                    stop, target = entry + 1.0, entry - 2.0
                signals.append(Signal(bar_idx=i, direction=direction,
                                        entry_price=entry, stop_price=stop,
                                        target_price=target))
            slip = float(rng.uniform(0.0, 0.3))
            comm = float(rng.uniform(0.0, 3.0))
            result = run_backtest(df, signals, starting_balance=100_000,
                                    lots=0.1, money_per_unit_price=1.0,
                                    commission_per_trade=comm,
                                    slippage_per_fill_atr_frac=slip)
            assert result.reconciles, (
                f"seed {seed} slip={slip:.3f} comm=${comm:.2f} FAILED: "
                f"diff={result.sum_realized_pnl - result.equity_curve_pnl:.6f}"
            )

    def test_partition_train_test_sums_to_total(self):
        """Train and test partitions must together sum to the total reconciled PnL."""
        rng = np.random.default_rng(123)
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=10, down_bars=10, n_cycles=8)
        signals = []
        for i in range(15, len(df) - 10, 17):
            direction = "LONG" if (i // 17) % 2 == 0 else "SHORT"
            entry = float(df["close"].iloc[i])
            if direction == "LONG":
                stop, target = entry - 1.0, entry + 2.0
            else:
                stop, target = entry + 1.0, entry - 2.0
            signals.append(Signal(bar_idx=i, direction=direction,
                                    entry_price=entry, stop_price=stop,
                                    target_price=target))
        result = run_backtest(df, signals, starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0,
                                commission_per_trade=1.0,
                                slippage_per_fill_atr_frac=0.1)
        assert result.reconciles
        for train_pct in (0.4, 0.5, 0.6, 0.7, 0.8):
            train, test = partition_train_test(result, train_pct, n_bars=len(df))
            assert train.n_trades + test.n_trades == result.n_trades
            assert abs((train.sum_pnl + test.sum_pnl) - result.sum_realized_pnl) < 1e-9, (
                f"train_pct={train_pct}: "
                f"train={train.sum_pnl} + test={test.sum_pnl} "
                f"!= total={result.sum_realized_pnl}"
            )

    def test_partition_rejects_invalid_train_pct(self):
        with pytest.raises(ValueError):
            partition_train_test(
                run_backtest(constant(price=100, n_bars=10), [],
                              starting_balance=100, lots=0.1,
                              money_per_unit_price=1.0),
                train_pct=0.0, n_bars=10,
            )
        with pytest.raises(ValueError):
            partition_train_test(
                run_backtest(constant(price=100, n_bars=10), [],
                              starting_balance=100, lots=0.1,
                              money_per_unit_price=1.0),
                train_pct=1.5, n_bars=10,
            )

    def test_eod_close_when_signal_at_last_bar(self):
        """REGRESSION: when a signal fires at bar n-1 and slippage>0, the
        position must be EOD-closed in the same iteration. Otherwise entry
        slippage leaks into equity_curve as unrealized floating PnL and
        reconciliation fails. (Found via grid sweep on 2026-05-03.)"""
        # Build candles where every bar has H-L = 1.0 → ATR(14) = 1.0 stable.
        n_bars = 30
        times = pd.date_range("2024-01-01", periods=n_bars, freq="h", tz="UTC")
        closes = np.full(n_bars, 100.0, dtype=float)
        candles = pd.DataFrame({
            "time": times, "open": closes,
            "high": closes + 0.5, "low": closes - 0.5,
            "close": closes, "volume": np.full(n_bars, 1000.0),
        })
        # Signal fires at the LAST bar
        sig = Signal(bar_idx=n_bars - 1, direction="LONG",
                      entry_price=100.0, stop_price=98.0, target_price=104.0)
        result = run_backtest(
            candles, [sig], starting_balance=100_000, lots=0.1,
            money_per_unit_price=1.0,
            slippage_per_fill_atr_frac=0.2,
        )
        # Reconciliation MUST hold even for last-bar signals with slippage.
        assert result.reconciles, (
            f"sum_pnl={result.sum_realized_pnl} eq_pnl={result.equity_curve_pnl} "
            f"diff={result.sum_realized_pnl - result.equity_curve_pnl}"
        )
        assert result.n_trades == 1
        t = result.trades[0]
        assert t.close_reason == "end_of_data"
        # Entry slipped UP, exit slipped DOWN — both at the SAME bar with same ATR.
        # PnL = (close - slip - entry - slip) × lots × $/unit = -2*slip*lots = -0.04
        assert abs(t.realized_pnl - (-0.04)) < 1e-9

    def test_slippage_reduces_pnl_vs_no_slippage(self):
        """Sanity: slippage should make the same winning trade LESS profitable."""
        candles = _ranged_trade_candles(
            entry_bar=5, entry_price=100.0, stop_price=95.0, target_price=102.0,
            n_bars=30, range_per_bar=0.5,
        )
        sig = Signal(bar_idx=5, direction="LONG",
                      entry_price=100.0, stop_price=95.0, target_price=102.0)
        no_slip = run_backtest(candles, [sig], starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0)
        with_slip = run_backtest(candles, [sig], starting_balance=100_000,
                                  lots=0.1, money_per_unit_price=1.0,
                                  slippage_per_fill_atr_frac=0.5)
        assert no_slip.trades[0].realized_pnl > with_slip.trades[0].realized_pnl
        assert with_slip.reconciles
