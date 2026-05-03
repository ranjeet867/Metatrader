"""
rsi_meanrev.py — RSI extremes mean-reversion.

LONG signal when RSI crosses ABOVE oversold (RSI was <= oversold last bar,
                                              now > oversold this bar)
SHORT signal when RSI crosses BELOW overbought.

Stop  = entry ± stop_atr_mult × ATR
Target = entry ± target_atr_mult × ATR
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder, rsi_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class RsiMeanRevParams:
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 2.0   # mean-rev typically uses smaller target
    long_only: bool = False


class RsiMeanRev:
    name = "rsi_meanrev"

    def __init__(self, params: RsiMeanRevParams = RsiMeanRevParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        if len(candles) < max(p.rsi_period, p.atr_period) + 2:
            return []

        rsi = rsi_wilder(candles["close"], p.rsi_period)
        atr = atr_wilder(candles, p.atr_period)

        out: list[Signal] = []
        c = candles["close"].values
        r = rsi.values
        atr_v = atr.values
        n = len(candles)

        for i in range(1, n):
            a = atr_v[i]
            if a <= 0 or pd.isna(r[i]) or pd.isna(r[i - 1]):
                continue
            # LONG: RSI crosses up through oversold
            if r[i - 1] <= p.oversold and r[i] > p.oversold:
                stop = c[i] - p.stop_atr_mult * a
                target = c[i] + p.target_atr_mult * a
                if stop > 0:
                    out.append(Signal(
                        bar_idx=i, direction="LONG",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason="rsi_oversold_cross_up",
                    ))
                continue
            if p.long_only:
                continue
            # SHORT: RSI crosses down through overbought
            if r[i - 1] >= p.overbought and r[i] < p.overbought:
                stop = c[i] + p.stop_atr_mult * a
                target = c[i] - p.target_atr_mult * a
                if target > 0:
                    out.append(Signal(
                        bar_idx=i, direction="SHORT",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason="rsi_overbought_cross_down",
                    ))
        return out
