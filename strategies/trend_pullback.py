"""
trend_pullback.py — designed for the GREEN ZONE of the Trendo R:R × WR
matrix. Targets ~3:1 R:R with ~35-45% win rate.

Signal idea (LONG):
  • Strong uptrend confirmed: EMA20 > EMA50 > EMA200 (triple-EMA stack)
  • Pullback: low touches within 1×ATR of EMA20
  • Reversal candle: bullish close in upper-third of bar's range
  • Wide target: 3× initial risk (configurable via target_R_mult)

The wide target is the whole point — we accept lower win rate (~40%)
in exchange for outsized winners. Math: 0.40 × 3 - 0.60 = +0.60R EV
per trade, well into the green zone.

Stop: bar low - 0.2 × ATR (a bit of pad below the swing low)
Target: entry + target_R_mult × stop_distance

LONG rules mirrored for SHORT when long_only=False.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class TrendPullbackParams:
    fast_period: int = 20
    medium_period: int = 50
    slow_period: int = 200
    atr_period: int = 14
    pullback_atr_mult: float = 1.0     # bar must touch within this × ATR of EMA20
    stop_atr_pad: float = 0.2          # stop = swing low - this × ATR
    target_R_mult: float = 3.0         # 3R target — green-zone aim
    long_only: bool = False


class TrendPullback:
    """Trend-pullback continuation. Targets 3:1 R:R / 35-45% WR — green
    zone of the Trendo profitability matrix."""

    name = "trend_pullback"

    def __init__(self, params: TrendPullbackParams = TrendPullbackParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        warmup = max(p.fast_period, p.medium_period, p.slow_period,
                       p.atr_period) + 2
        if len(candles) < warmup:
            return []

        ef = ema(candles["close"], p.fast_period)
        em = ema(candles["close"], p.medium_period)
        es = ema(candles["close"], p.slow_period)
        atr = atr_wilder(candles, p.atr_period)

        out: list[Signal] = []
        c = candles["close"].values
        o = candles["open"].values
        h = candles["high"].values
        l = candles["low"].values
        ef_v = ef.values
        em_v = em.values
        es_v = es.values
        atr_v = atr.values
        n = len(candles)

        for i in range(warmup, n):
            a = atr_v[i]
            if a <= 0:
                continue

            bar_range = h[i] - l[i]
            if bar_range <= 0:
                continue

            # ----- LONG: triple-EMA uptrend, pullback to EMA20, reversal -----
            uptrend = (ef_v[i] > em_v[i]
                        and em_v[i] > es_v[i]
                        and c[i] > es_v[i])
            pulled_back_long = (l[i] <= ef_v[i] + p.pullback_atr_mult * a)
            # Bullish bar where close is in upper third of range
            close_in_upper_third = (c[i] >= l[i] + 0.66 * bar_range)
            bullish_bar = (c[i] > o[i])

            if uptrend and pulled_back_long and bullish_bar and close_in_upper_third:
                stop = l[i] - p.stop_atr_pad * a
                if stop <= 0 or stop >= c[i]:
                    continue
                risk = c[i] - stop
                if risk <= 0:
                    continue
                target = c[i] + p.target_R_mult * risk
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=float(c[i]), stop_price=float(stop),
                    target_price=float(target),
                    reason="trend_pullback_3R",
                ))
                continue
            if p.long_only:
                continue

            # ----- SHORT: triple-EMA downtrend, rally to EMA20, reversal -----
            downtrend = (ef_v[i] < em_v[i]
                          and em_v[i] < es_v[i]
                          and c[i] < es_v[i])
            rallied_back_short = (h[i] >= ef_v[i] - p.pullback_atr_mult * a)
            close_in_lower_third = (c[i] <= h[i] - 0.66 * bar_range)
            bearish_bar = (c[i] < o[i])

            if downtrend and rallied_back_short and bearish_bar and close_in_lower_third:
                stop = h[i] + p.stop_atr_pad * a
                if stop <= c[i]:
                    continue
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
                    reason="trend_pullback_3R_short",
                ))

        return out
