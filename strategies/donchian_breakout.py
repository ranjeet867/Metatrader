"""
donchian_breakout.py — close breaks the prior N-period extreme.

LONG signal when close > rolling_high(N).shift(1) — i.e. above the highest
close of the prior N bars (excluding current).

Stop  = entry - stop_atr_mult × ATR
Target = entry + target_atr_mult × ATR
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder, rolling_high, rolling_low
from core.strategy import Signal


@dataclass(frozen=True)
class DonchianBreakoutParams:
    period: int = 20
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 3.0   # → 2R reward when stop_atr_mult=1.5
    long_only: bool = False


class DonchianBreakout:
    name = "donchian_breakout"

    def __init__(self, params: DonchianBreakoutParams = DonchianBreakoutParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        if len(candles) < max(p.period, p.atr_period) + 2:
            return []

        # Use prior bars only (shift(1) excludes the current bar)
        prior_hi = rolling_high(candles["close"], p.period).shift(1)
        prior_lo = rolling_low(candles["close"], p.period).shift(1)
        atr = atr_wilder(candles, p.atr_period)

        out: list[Signal] = []
        c = candles["close"].values
        ph = prior_hi.values
        pl = prior_lo.values
        atr_v = atr.values
        n = len(candles)

        # Track whether we already fired on this "leg" so we don't repeat
        # signals for every bar above the broken level
        for i in range(1, n):
            a = atr_v[i]
            if a <= 0 or pd.isna(ph[i]) or pd.isna(pl[i]):
                continue
            # Cross above: prior bar at-or-below prior high, current bar above
            if c[i] > ph[i] and c[i - 1] <= (ph[i - 1] if not pd.isna(ph[i - 1]) else c[i] + 1):
                stop = c[i] - p.stop_atr_mult * a
                target = c[i] + p.target_atr_mult * a
                if stop > 0:
                    out.append(Signal(
                        bar_idx=i, direction="LONG",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason=f"donchian_{p.period}_high_break",
                    ))
                continue
            if p.long_only:
                continue
            # Cross below
            if c[i] < pl[i] and c[i - 1] >= (pl[i - 1] if not pd.isna(pl[i - 1]) else c[i] - 1):
                stop = c[i] + p.stop_atr_mult * a
                target = c[i] - p.target_atr_mult * a
                if target > 0:
                    out.append(Signal(
                        bar_idx=i, direction="SHORT",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason=f"donchian_{p.period}_low_break",
                    ))
        return out
