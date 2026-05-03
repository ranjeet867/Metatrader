"""
bbands_meanrev.py — Bollinger band mean-reversion.

LONG signal when:
  - prior bar's close was BELOW lower band
  - current bar's close is BACK INSIDE the bands (close > lower band)

SHORT signal: mirror.

Stop  = entry ± stop_atr_mult × ATR
Target = mid band                           (the natural reversion target)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder, bollinger_bands
from core.strategy import Signal


@dataclass(frozen=True)
class BBandsMeanRevParams:
    bb_period: int = 20
    bb_k: float = 2.0
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    long_only: bool = False


class BBandsMeanRev:
    name = "bbands_meanrev"

    def __init__(self, params: BBandsMeanRevParams = BBandsMeanRevParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        if len(candles) < max(p.bb_period, p.atr_period) + 2:
            return []

        upper, mid, lower = bollinger_bands(candles["close"], p.bb_period, p.bb_k)
        atr = atr_wilder(candles, p.atr_period)

        out: list[Signal] = []
        c = candles["close"].values
        u = upper.values
        m = mid.values
        l = lower.values
        atr_v = atr.values
        n = len(candles)

        for i in range(1, n):
            a = atr_v[i]
            if (a <= 0 or pd.isna(u[i]) or pd.isna(m[i]) or pd.isna(l[i])
                    or pd.isna(u[i - 1]) or pd.isna(l[i - 1])):
                continue
            # LONG: prior close below lower band, this close back above lower band
            if c[i - 1] < l[i - 1] and c[i] > l[i] and c[i] < m[i]:
                stop = c[i] - p.stop_atr_mult * a
                target = m[i]   # natural reversion target
                if stop > 0 and target > c[i]:
                    out.append(Signal(
                        bar_idx=i, direction="LONG",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason="bb_lower_band_revert",
                    ))
                continue
            if p.long_only:
                continue
            # SHORT: prior close above upper band, this close back below upper band
            if c[i - 1] > u[i - 1] and c[i] < u[i] and c[i] > m[i]:
                stop = c[i] + p.stop_atr_mult * a
                target = m[i]
                if target < c[i]:
                    out.append(Signal(
                        bar_idx=i, direction="SHORT",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason="bb_upper_band_revert",
                    ))
        return out
