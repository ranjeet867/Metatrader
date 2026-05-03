"""
ema_pullback.py — pullback to EMA20 in a confirmed trend.

LONG signal when ALL of:
  - close > EMA(slow_period)        — trend filter (price in uptrend)
  - low touched within `pullback_atr_mult × ATR` of EMA(fast_period)
  - this bar is bullish (close > open) — confirmation

Stop  = bar low - 0.1 × ATR
Target = entry + target_atr_mult × stop_distance       (so target = N × R)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class EmaPullbackParams:
    fast_period: int = 20
    slow_period: int = 50
    atr_period: int = 14
    pullback_atr_mult: float = 0.5     # bar low must be within N×ATR of EMA fast
    stop_atr_pad: float = 0.1          # stop = bar low - this × ATR
    target_R_mult: float = 2.0         # target = N × stop_distance from entry
    long_only: bool = False


class EmaPullback:
    name = "ema_pullback"

    def __init__(self, params: EmaPullbackParams = EmaPullbackParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        if len(candles) < max(p.fast_period, p.slow_period, p.atr_period) + 2:
            return []

        ef = ema(candles["close"], p.fast_period)
        es = ema(candles["close"], p.slow_period)
        atr = atr_wilder(candles, p.atr_period)

        out: list[Signal] = []
        c = candles["close"].values
        o = candles["open"].values
        h = candles["high"].values
        l = candles["low"].values
        ef_v = ef.values
        es_v = es.values
        atr_v = atr.values
        n = len(candles)

        for i in range(1, n):
            a = atr_v[i]
            if a <= 0:
                continue
            # ----- LONG -----
            if (c[i] > es_v[i]                              # uptrend
                and l[i] <= ef_v[i] + p.pullback_atr_mult * a
                and c[i] > o[i]):                            # bullish bar
                stop = l[i] - p.stop_atr_pad * a
                if stop <= 0:
                    continue
                risk = c[i] - stop
                if risk <= 0:
                    continue
                target = c[i] + p.target_R_mult * risk
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=float(c[i]), stop_price=float(stop),
                    target_price=float(target),
                    reason="pullback_to_ema_in_uptrend",
                ))
                continue
            if p.long_only:
                continue
            # ----- SHORT -----
            if (c[i] < es_v[i]                              # downtrend
                and h[i] >= ef_v[i] - p.pullback_atr_mult * a
                and c[i] < o[i]):                            # bearish bar
                stop = h[i] + p.stop_atr_pad * a
                risk = stop - c[i]
                if risk <= 0:
                    continue
                target = c[i] - p.target_R_mult * risk
                if target <= 0:
                    continue
                out.append(Signal(
                    bar_idx=i, direction="SHORT",
                    entry_price=float(c[i]), stop_price=float(stop),
                    target_price=float(target),
                    reason="pullback_to_ema_in_downtrend",
                ))
        return out
