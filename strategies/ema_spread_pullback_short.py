"""
ema_spread_pullback_short.py — bearish mirror of ema_spread_pullback.

Setup (SHORT — fade rallies in clear downtrend):
  1. EMA9 < EMA21 < EMA50  (clean downtrend)
  2. (EMA50 − EMA9) / close > spread_threshold  (fanned, not clustered)
  3. Rally: bar high tags within touch_pct of EMA21
  4. Bearish confirmation: close < open AND close < EMA9
  Stop = bar high + stop_atr_pad × ATR
  Target = entry − rr_target × stop_distance
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class EmaSpreadPullbackShortParams:
    fast_period: int = 9
    mid_period: int = 21
    slow_period: int = 50
    spread_threshold: float = 0.005
    touch_pct: float = 0.003
    stop_atr_pad: float = 0.4
    atr_period: int = 14
    rr_target: float = 2.0
    long_only: bool = False           # always False here


class EmaSpreadPullbackShort:
    name = "ema_spread_pullback_short"

    def __init__(self, params: EmaSpreadPullbackShortParams
                 = EmaSpreadPullbackShortParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < max(p.slow_period, p.atr_period) + 5:
            return []
        e9 = ema(candles["close"], p.fast_period).values
        e21 = ema(candles["close"], p.mid_period).values
        e50 = ema(candles["close"], p.slow_period).values
        atr = atr_wilder(candles, p.atr_period).values
        c = candles["close"].values
        o = candles["open"].values
        h = candles["high"].values
        l = candles["low"].values
        spread = (e50 - e9) / np.where(c > 0, c, 1.0)

        out: list[Signal] = []
        for i in range(p.slow_period + 5, n):
            if np.isnan(e50[i]) or atr[i] <= 0:
                continue
            # Clean downtrend: fast < mid < slow
            if not (e9[i] < e21[i] < e50[i]):
                continue
            if spread[i] < p.spread_threshold:
                continue
            # Rally to EMA21
            touched = (abs(h[i] - e21[i]) / e21[i] < p.touch_pct
                       or l[i] <= e21[i] <= h[i])
            bearish = (c[i] < o[i] and c[i] < e9[i])
            if not (touched and bearish):
                continue
            stop = h[i] + p.stop_atr_pad * atr[i]
            if stop <= c[i]:
                continue
            risk = stop - c[i]
            target = c[i] - p.rr_target * risk
            if target <= 0:
                continue
            out.append(Signal(
                bar_idx=i, direction="SHORT",
                entry_price=float(c[i]),
                stop_price=float(stop),
                target_price=float(target),
                reason="ema_spread_pullback_short",
            ))
        return out
