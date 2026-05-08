"""
ema_spread_pullback.py — trend pullback gated by EMA-fan separation.

Avoids the chop. Trades only when EMA9 / EMA21 / EMA50 are clearly
separated and ordered. The "spread" is (EMA9 − EMA50) / close. When
that's small the EMAs are clustered and price is sideways — we sit
out. When it's large there's a genuine trend to ride.

Setup (LONG only):
  1. EMA9 > EMA21 > EMA50  (clean uptrend)
  2. (EMA9 − EMA50) / close > spread_threshold  (default 0.5%)
  3. Pullback: bar low touches within touch_pct of EMA21 (default 0.3%)
  4. Bounce confirmation: close > open AND close > EMA9
  Stop = bar low − stop_atr_pad × ATR
  Target = entry + rr_target × stop_distance

Empirically best cells (optimization_2026-05-06):
  - XAUUSD D1 R:R 1:3 → PF 2.23, mean_R +0.85, +$44k, 12.6% DD
  - XPDUSD D1 R:R 1:3 → PF 2.77, mean_R +1.26, +$44k, 18.2% DD (sat)
  - XPTUSD D1 R:R 1:3 → PF 2.21, +$25k, 5.4% DD (clean)

This unlocks GOLD trading — pure 9-EMA pullback didn't work on XAUUSD,
but adding the spread requirement filtered out the chop-trades and the
edge emerged.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class EmaSpreadPullbackParams:
    fast_period: int = 9
    mid_period: int = 21
    slow_period: int = 50
    spread_threshold: float = 0.005    # (ema9 - ema50) / close > 0.5%
    touch_pct: float = 0.003           # within 0.3% of EMA21
    stop_atr_pad: float = 0.4
    atr_period: int = 14
    rr_target: float = 2.0             # 1:1, 1:2, 1:3 supported
    long_only: bool = True             # original sweep was long-only


class EmaSpreadPullback:
    name = "ema_spread_pullback"

    def __init__(self, params: EmaSpreadPullbackParams
                 = EmaSpreadPullbackParams()):
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
        spread = (e9 - e50) / np.where(c > 0, c, 1.0)

        out: list[Signal] = []
        for i in range(p.slow_period + 5, n):
            if np.isnan(e50[i]) or atr[i] <= 0:
                continue
            # Clean uptrend with fanned EMAs
            if not (e9[i] > e21[i] > e50[i]):
                continue
            if spread[i] < p.spread_threshold:
                continue
            # Pullback to EMA21
            touched = (abs(l[i] - e21[i]) / e21[i] < p.touch_pct
                       or l[i] <= e21[i] <= h[i])
            bounced = (c[i] > o[i] and c[i] > e9[i])
            if not (touched and bounced):
                continue
            stop = l[i] - p.stop_atr_pad * atr[i]
            if stop <= 0 or stop >= c[i]:
                continue
            risk = c[i] - stop
            target = c[i] + p.rr_target * risk
            out.append(Signal(
                bar_idx=i, direction="LONG",
                entry_price=float(c[i]),
                stop_price=float(stop),
                target_price=float(target),
                reason="ema_spread_pullback",
            ))
        return out
