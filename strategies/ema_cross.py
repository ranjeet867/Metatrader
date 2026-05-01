"""
ema_cross.py — the ONE locked strategy for v2.

Rule:
  LONG signal when EMA(fast) crosses ABOVE EMA(slow) on the closed bar.
  SHORT signal when EMA(fast) crosses BELOW EMA(slow) on the closed bar.

Stop = entry - stop_atr_mult × ATR(atr_period)   [LONG; mirror for SHORT]
Target = entry + target_atr_mult × ATR(atr_period)   [LONG; mirror for SHORT]

Why this strategy first:
  - Trivially testable. With known data we can predict every signal.
  - One of the most studied/written-about strategies in literature.
  - Easy to port to MQL5 for MT5 Strategy Tester parity check.
  - If THIS doesn't work, simpler doesn't either.

Inputs are pure: same candles + same params → same Signals every time.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class EmaCrossParams:
    fast_period: int = 9
    slow_period: int = 20
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 3.0   # → 2R reward when stop_atr_mult=1.5
    long_only: bool = False        # if True, ignore bearish crosses


class EmaCross:
    name = "ema_cross"

    def __init__(self, params: EmaCrossParams = EmaCrossParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        """Detect crossovers on CLOSED bars and emit signals."""
        p = self.params
        if len(candles) < max(p.fast_period, p.slow_period, p.atr_period) + 2:
            return []

        ema_fast = ema(candles["close"], p.fast_period)
        ema_slow = ema(candles["close"], p.slow_period)
        atr = atr_wilder(candles, p.atr_period)

        # Cross detection on the CLOSE of bar i: previous bar fast<=slow,
        # this bar fast>slow. Signal fires AT bar i (entry at bar i's close).
        out: list[Signal] = []
        n = len(candles)
        for i in range(1, n):
            f_prev = ema_fast.iloc[i - 1]
            s_prev = ema_slow.iloc[i - 1]
            f_now = ema_fast.iloc[i]
            s_now = ema_slow.iloc[i]
            entry = float(candles["close"].iloc[i])
            atr_val = float(atr.iloc[i])
            if atr_val <= 0:
                continue

            # LONG cross: was below-or-equal, now strictly above
            if f_prev <= s_prev and f_now > s_now:
                stop = entry - p.stop_atr_mult * atr_val
                target = entry + p.target_atr_mult * atr_val
                if stop > 0:
                    out.append(Signal(
                        bar_idx=i, direction="LONG",
                        entry_price=entry, stop_price=stop, target_price=target,
                        reason=f"ema{p.fast_period}_above_ema{p.slow_period}",
                    ))
            # SHORT cross: was above-or-equal, now strictly below
            elif (not p.long_only) and f_prev >= s_prev and f_now < s_now:
                stop = entry + p.stop_atr_mult * atr_val
                target = entry - p.target_atr_mult * atr_val
                if target > 0:
                    out.append(Signal(
                        bar_idx=i, direction="SHORT",
                        entry_price=entry, stop_price=stop, target_price=target,
                        reason=f"ema{p.fast_period}_below_ema{p.slow_period}",
                    ))
        return out
