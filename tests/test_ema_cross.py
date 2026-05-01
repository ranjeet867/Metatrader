"""
test_ema_cross.py — verify the strategy fires signals at PREDICTABLE bars
for hand-crafted candle series.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ema_cross import EmaCross, EmaCrossParams
from tests.fixtures.synthetic import constant, step_function, sawtooth


class TestNoSignalOnFlatData:
    """Constant prices → no EMA cross → no signals."""

    def test_constant_data_yields_zero_signals(self):
        df = constant(price=100, n_bars=200)
        s = EmaCross(EmaCrossParams(fast_period=9, slow_period=20))
        assert s.signals(df) == []


class TestExactlyOneCrossOnStep:
    """A step from low to high → EXACTLY ONE bullish crossover.

    With bars_at_low=100 (>> slow_period), both EMAs converge to low_price
    before the step. After the step, EMA9 (faster) crosses EMA20 within
    a known window."""

    def test_step_up_yields_one_long_signal(self):
        df = step_function(low_price=100, high_price=110,
                            bars_at_low=100, bars_at_high=100)
        s = EmaCross(EmaCrossParams(fast_period=9, slow_period=20,
                                       atr_period=14,
                                       stop_atr_mult=1.5, target_atr_mult=3.0))
        sigs = s.signals(df)
        # The step is at bar 100. EMA9 crosses EMA20 from below at some bar
        # shortly after — should be exactly 1 bullish crossover (LONG signal).
        long_signals = [x for x in sigs if x.direction == "LONG"]
        short_signals = [x for x in sigs if x.direction == "SHORT"]
        assert len(long_signals) == 1, \
            f"expected 1 LONG signal, got {len(long_signals)}: {long_signals}"
        # The LONG should fire on or shortly after the step (the step IS at bar 100)
        assert 100 <= long_signals[0].bar_idx < 110, \
            f"LONG signal at bar {long_signals[0].bar_idx}, expected near 100"
        # And SHORT signals: zero (we only step up, never down)
        assert len(short_signals) == 0


class TestSignalGeometryCorrect:
    """For a LONG signal: stop < entry < target. Mirror for SHORT."""

    def test_long_signal_geometry(self):
        df = step_function(low_price=100, high_price=110,
                            bars_at_low=100, bars_at_high=100)
        s = EmaCross(EmaCrossParams())
        for sig in s.signals(df):
            if sig.direction == "LONG":
                assert sig.stop_price < sig.entry_price < sig.target_price, \
                    f"bad LONG geometry: {sig}"
            else:
                assert sig.target_price < sig.entry_price < sig.stop_price, \
                    f"bad SHORT geometry: {sig}"


class TestIdempotency:
    """Same candles + same params → identical signal list every time."""

    def test_signals_idempotent(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=8)
        s = EmaCross(EmaCrossParams())
        a = s.signals(df)
        b = s.signals(df)
        assert len(a) == len(b)
        for x, y in zip(a, b):
            assert x == y


class TestLongOnlyMode:
    """When long_only=True, no SHORT signals are emitted."""

    def test_long_only_filters_shorts(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)
        all_signals = EmaCross(EmaCrossParams(long_only=False)).signals(df)
        long_only = EmaCross(EmaCrossParams(long_only=True)).signals(df)
        assert all(s.direction == "LONG" for s in long_only)
        assert len([s for s in all_signals if s.direction == "SHORT"]) > 0
        assert len([s for s in long_only]) <= len([s for s in all_signals if s.direction == "LONG"])
        # The LONG signals should be the same set
        assert {s.bar_idx for s in long_only} == \
               {s.bar_idx for s in all_signals if s.direction == "LONG"}


class TestSawtoothProducesAlternatingSignals:
    """A clean sawtooth pattern produces alternating LONG/SHORT crosses
    over its cycles. We don't predict the exact bar count, but we DO
    assert structural properties."""

    def test_sawtooth_signals_alternate(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=20, down_bars=20, n_cycles=10)
        sigs = EmaCross(EmaCrossParams()).signals(df)
        assert len(sigs) > 0, "sawtooth should produce some signals"
        # Signals must be in time order
        for i in range(1, len(sigs)):
            assert sigs[i].bar_idx > sigs[i - 1].bar_idx
        # And alternate direction (no two consecutive same direction)
        for i in range(1, len(sigs)):
            assert sigs[i].direction != sigs[i - 1].direction, \
                f"two consecutive same-direction signals at idx {i}"
