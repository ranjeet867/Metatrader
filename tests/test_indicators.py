"""
test_indicators.py — verify indicator math against closed-form expected values.

Every test asserts an EXACT mathematical property. If any of these fail,
indicator math is wrong and no downstream result can be trusted.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.indicators import ema, true_range, atr_wilder
from tests.fixtures.synthetic import constant, linear_ramp, step_function


# ===========================================================================
# EMA — closed-form properties
# ===========================================================================
class TestEMA:
    def test_ema_of_constant_is_constant(self):
        """EMA of a flat series equals the constant. Always."""
        df = constant(price=100.0, n_bars=200)
        e = ema(df["close"], 9)
        assert np.allclose(e.values, 100.0, atol=1e-9), \
            f"EMA(constant=100) should be 100; got first 5: {e.values[:5]}"

    def test_ema_first_value_equals_first_input(self):
        """With adjust=False, EMA[0] == input[0]."""
        df = linear_ramp(start_price=100.0, step=0.5, n_bars=50)
        e = ema(df["close"], 9)
        assert e.iloc[0] == df["close"].iloc[0]

    def test_ema_recurrence_matches_formula(self):
        """Verify EMA[i] = alpha × input[i] + (1-alpha) × EMA[i-1] exactly."""
        df = linear_ramp(start_price=100.0, step=0.5, n_bars=50)
        period = 9
        alpha = 2.0 / (period + 1)
        prices = df["close"].values
        e = ema(df["close"], period).values

        # Build the expected series from scratch using the formula
        expected = np.zeros_like(prices)
        expected[0] = prices[0]
        for i in range(1, len(prices)):
            expected[i] = alpha * prices[i] + (1 - alpha) * expected[i - 1]

        assert np.allclose(e, expected, atol=1e-9)

    def test_ema_period_validation(self):
        """period < 1 must raise."""
        df = constant()
        with pytest.raises(ValueError):
            ema(df["close"], 0)
        with pytest.raises(ValueError):
            ema(df["close"], -5)

    def test_ema_step_response_known_formula(self):
        """
        After a step from 100 to 110, the EMA at bar (transition + k) is:
            E[k] = 110 - 10 × (1 - alpha)^(k+1)
        where alpha = 2 / (period + 1).

        We use bars_at_low = 100 to ensure EMA fully converges to 100 first.
        """
        period = 9
        alpha = 2.0 / (period + 1)
        df = step_function(low_price=100, high_price=110,
                            bars_at_low=100, bars_at_high=100)
        e = ema(df["close"], period).values

        # Right before the step: should be exactly 100
        assert abs(e[99] - 100.0) < 1e-6, f"pre-step EMA != 100: {e[99]}"

        # k bars after the step (transition is at bar 100):
        for k in range(0, 10):
            expected = 110 - 10 * (1 - alpha) ** (k + 1)
            actual = e[100 + k]
            assert abs(actual - expected) < 1e-6, \
                f"bar {100+k} (k={k}): expected {expected:.6f}, got {actual:.6f}"


# ===========================================================================
# True Range — verify the max-of-three formula
# ===========================================================================
class TestTrueRange:
    def test_tr_constant_is_zero(self):
        """If H=L=C every bar, TR = 0 for all bars."""
        df = constant(price=100, n_bars=50)
        tr = true_range(df)
        assert np.allclose(tr.values, 0.0, atol=1e-12)

    def test_tr_linear_ramp_with_zero_range(self):
        """linear_ramp has H=L=close per bar, but close changes between bars.
        So TR[0] = 0 (first bar, H-L=0), and
        TR[i>0] = max(0, |C[i] - C[i-1]|, |C[i] - C[i-1]|) = step.
        """
        step = 0.5
        df = linear_ramp(start_price=100, step=step, n_bars=20)
        tr = true_range(df)
        assert tr.iloc[0] == 0.0
        for i in range(1, len(df)):
            assert abs(tr.iloc[i] - step) < 1e-9, \
                f"bar {i}: TR should be {step}, got {tr.iloc[i]}"

    def test_tr_first_bar_is_high_minus_low(self):
        """When there's no prior close, TR = H - L."""
        df = pd.DataFrame({
            "time": pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC"),
            "open": [100, 100, 100], "high": [105, 102, 103],
            "low":  [98, 99, 97], "close": [100, 100, 100],
            "volume": [1, 1, 1],
        })
        tr = true_range(df)
        assert tr.iloc[0] == 7.0   # 105 - 98


# ===========================================================================
# ATR (Wilder) — closed-form properties
# ===========================================================================
class TestATRWilder:
    def test_atr_constant_is_zero(self):
        """No range → no ATR."""
        df = constant(price=100, n_bars=100)
        a = atr_wilder(df, 14)
        assert np.allclose(a.values, 0.0, atol=1e-12)

    def test_atr_steady_step_converges_to_step(self):
        """
        On a linear_ramp where TR = step every bar (after the first),
        Wilder's ATR converges to `step` exponentially.

        After many bars, ATR ≈ step. We assert ATR is within 1% of step
        once we're far enough past the initial transient.
        """
        step = 0.5
        period = 14
        df = linear_ramp(start_price=100, step=step, n_bars=200)
        a = atr_wilder(df, period).values

        # After 10× period bars, should be very close to `step`
        assert abs(a[-1] - step) < step * 0.001, \
            f"ATR didn't converge to {step}: got {a[-1]}"

    def test_atr_recurrence_matches_formula(self):
        """ATR[i] = alpha × TR[i] + (1 - alpha) × ATR[i-1], alpha = 1/period."""
        df = linear_ramp(start_price=100, step=0.3, n_bars=80)
        period = 14
        alpha = 1.0 / period
        tr = true_range(df).values
        a = atr_wilder(df, period).values

        expected = np.zeros_like(tr)
        expected[0] = tr[0]
        for i in range(1, len(tr)):
            expected[i] = alpha * tr[i] + (1 - alpha) * expected[i - 1]

        assert np.allclose(a, expected, atol=1e-12)

    def test_atr_period_validation(self):
        df = constant()
        with pytest.raises(ValueError):
            atr_wilder(df, 0)


# ===========================================================================
# Idempotency — same input must always produce same output
# ===========================================================================
class TestIdempotency:
    def test_ema_idempotent(self):
        df = linear_ramp(n_bars=100)
        a = ema(df["close"], 14).values
        b = ema(df["close"], 14).values
        assert np.array_equal(a, b)

    def test_atr_idempotent(self):
        df = linear_ramp(n_bars=100)
        a = atr_wilder(df, 14).values
        b = atr_wilder(df, 14).values
        assert np.array_equal(a, b)
