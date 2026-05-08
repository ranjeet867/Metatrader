"""
engulfing_at_extreme.py — bullish/bearish engulfing candle at an RSI
extreme. Combines candlestick reversal + momentum exhaustion.

LONG rule:
  - At bar i, RSI[i-1] < oversold (default 30)
  - Bar i is a bullish engulfing of bar i-1:
      * close[i] > open[i]   (green)
      * open[i-1] > close[i-1]  (prior was red)
      * close[i] >= open[i-1]   (engulfs body high)
      * open[i] <= close[i-1]   (engulfs body low)
  - Enter LONG at close[i]
  - Stop = low[i] - stop_atr_mult × ATR
  - Target = entry + target_R_mult × (entry - stop)

SHORT rule (mirror):
  - RSI[i-1] > overbought (default 70)
  - Bearish engulfing
  - Enter SHORT at close[i]

Why this is interesting:
  - Pure RSI-mean-rev fires on RSI extremes alone — high frequency but mixed
    quality (fades a strong trend). Adding the engulfing requirement filters
    to bars where the market has already shown a reversal candle, which
    historically has materially better follow-through stats.
  - Engulfing alone fires too often. The RSI gate cuts trade count by ~80%
    while preserving the high-quality setups.
  - Different signal kernel from anything in your catalog — diversifies.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import rsi_wilder, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class EngulfingAtExtremeParams:
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    require_extreme_on_prior: bool = True  # RSI[i-1] in extreme zone, not RSI[i]
    body_engulf_only: bool = True   # ignore wicks for engulfing comparison
    atr_period: int = 14
    stop_atr_mult: float = 0.5     # buffer below low / above high
    target_R_mult: float = 2.0
    long_only: bool = False        # bidir by default
    regime_ema_period: int = 0     # 0 = no regime filter; >0 = only LONG when
                                    # close > EMA(p), only SHORT when below


class EngulfingAtExtreme:
    name = "engulfing_at_extreme"

    def __init__(self, params: EngulfingAtExtremeParams = EngulfingAtExtremeParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        warmup = max(p.rsi_period * 3, p.atr_period * 3,
                     p.regime_ema_period * 2, 50)
        if n < warmup + 2:
            return []

        o = candles["open"].to_numpy()
        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()
        c = candles["close"].to_numpy()
        rsi = rsi_wilder(candles["close"], p.rsi_period).to_numpy()
        atr = atr_wilder(candles, p.atr_period).to_numpy()
        if p.regime_ema_period > 0:
            from core.indicators import ema
            regime = ema(candles["close"], p.regime_ema_period).to_numpy()
        else:
            regime = None

        out: list[Signal] = []
        for i in range(max(warmup, 1), n):
            r_prev = rsi[i - 1]
            if np.isnan(r_prev):
                continue
            atr_i = atr[i]
            if np.isnan(atr_i) or atr_i <= 0:
                continue

            # -- LONG: bullish engulfing at oversold --
            if r_prev < p.oversold:
                if regime is not None and c[i] <= regime[i]:
                    pass    # blocked by regime filter
                else:
                    prior_red = o[i - 1] > c[i - 1]
                    cur_green = c[i] > o[i]
                    if prior_red and cur_green:
                        if p.body_engulf_only:
                            engulfs = (c[i] >= o[i - 1]) and (o[i] <= c[i - 1])
                        else:
                            engulfs = (h[i] >= h[i - 1]) and (l[i] <= l[i - 1])
                        if engulfs:
                            entry = float(c[i])
                            stop = float(l[i]) - p.stop_atr_mult * float(atr_i)
                            risk = entry - stop
                            if risk > 0:
                                target = entry + p.target_R_mult * risk
                                out.append(Signal(
                                    bar_idx=i, direction="LONG",
                                    entry_price=entry, stop_price=stop,
                                    target_price=target,
                                    reason="bull_engulf_oversold",
                                ))
                                continue

            # -- SHORT: bearish engulfing at overbought --
            if p.long_only:
                continue
            if r_prev > p.overbought:
                if regime is not None and c[i] >= regime[i]:
                    continue
                prior_green = c[i - 1] > o[i - 1]
                cur_red = c[i] < o[i]
                if prior_green and cur_red:
                    if p.body_engulf_only:
                        engulfs = (o[i] >= c[i - 1]) and (c[i] <= o[i - 1])
                    else:
                        engulfs = (h[i] >= h[i - 1]) and (l[i] <= l[i - 1])
                    if engulfs:
                        entry = float(c[i])
                        stop = float(h[i]) + p.stop_atr_mult * float(atr_i)
                        risk = stop - entry
                        if risk > 0:
                            target = entry - p.target_R_mult * risk
                            if target > 0:
                                out.append(Signal(
                                    bar_idx=i, direction="SHORT",
                                    entry_price=entry, stop_price=stop,
                                    target_price=target,
                                    reason="bear_engulf_overbought",
                                ))
        return out
