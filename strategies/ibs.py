"""
ibs.py — Internal Bar Strength (IBS) mean-reversion.

  IBS[i] = (close[i] - low[i]) / (high[i] - low[i])

Rule (Connors-style, adapted to our SL/TP+max_hold framework):

  LONG signal at bar i when ALL of:
    * IBS[i-1] < ibs_oversold          (prior bar closed near its low)
    * close[i] > close[i-1]             (today closed up)
    * high[i] > low[i]                  (skip pathological zero-range bars)

  Exit:
    * target = entry + bounce_atr_mult × ATR(atr_period)   (small bounce)
    * stop   = entry - stop_atr_mult   × ATR(atr_period)   (wider stop)
    * max_hold_bars = max_hold          (force-close after N bars if neither)

The original Connors rule is "exit on first bar where IBS > 0.50". Our backtester
has SL/TP/time exits, not custom-condition exits, so we use a small-bounce target
+ time-cap as a faithful approximation. (When IBS recovers, price typically
closes in upper half of range, which usually corresponds to a small bounce.)

Documented edge on equity indices in Connors, "Short-Term Trading Strategies
That Work". Should NOT work on FX (no statistical regularity in close-vs-range
on FX intraday).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class IbsParams:
    ibs_oversold: float = 0.20      # prior bar IBS must be below this
    atr_period: int = 14
    bounce_atr_mult: float = 0.5    # target = entry + bounce × ATR
    stop_atr_mult: float = 2.0      # stop = entry - stop × ATR
    max_hold: int = 5               # force-close after N bars
    long_only: bool = True          # IBS edge is one-sided in equities literature


class Ibs:
    name = "ibs"

    def __init__(self, params: IbsParams = IbsParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < max(p.atr_period + 2, 3):
            return []

        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()
        c = candles["close"].to_numpy()
        atr = atr_wilder(candles, p.atr_period).to_numpy()

        rng = h - l
        valid = rng > 0
        ibs = np.zeros(n, dtype=float)
        ibs[valid] = (c[valid] - l[valid]) / rng[valid]

        out: list[Signal] = []
        for i in range(2, n):
            a = atr[i]
            if a <= 0:
                continue
            if not valid[i - 1]:
                continue
            if ibs[i - 1] >= p.ibs_oversold:
                continue
            if c[i] <= c[i - 1]:
                continue
            entry = float(c[i])
            stop = entry - p.stop_atr_mult * a
            target = entry + p.bounce_atr_mult * a
            if stop <= 0:
                continue
            out.append(Signal(
                bar_idx=i, direction="LONG",
                entry_price=entry, stop_price=float(stop),
                target_price=float(target),
                reason="ibs_oversold_and_up_close",
                max_hold_bars=p.max_hold,
            ))
        return out
