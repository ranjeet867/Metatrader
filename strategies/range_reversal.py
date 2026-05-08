"""
range_reversal.py — designed for the GREEN ZONE of the Trendo matrix
via the OPPOSITE corner: high win rate at moderate R:R.

Targets ~55-65% win rate at ~1.5:1 R:R.
Math: 0.55 × 1.5 - 0.45 = +0.375R EV per trade — green zone.

Signal idea:
  • Identify range condition: ADX-proxy via flat EMA20 (slope < 0.1×ATR)
  • Bollinger Band lower-touch + RSI oversold → LONG
  • Bollinger Band upper-touch + RSI overbought → SHORT
  • Reversal candle confirmation (close back inside band)
  • Stop: just past the band touch
  • Target: middle Bollinger band (mid-range mean reversion)

This is anti-trend by design — it's NOT the same as bbands_meanrev
which uses tighter stops. Here we widen the stop slightly and target
the mid-band specifically for predictable mean-reversion behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder, bollinger_bands, ema
from core.strategy import Signal


@dataclass(frozen=True)
class RangeReversalParams:
    bb_period: int = 20
    bb_k: float = 2.0
    atr_period: int = 14
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    range_slope_atr: float = 0.10      # EMA slope must be < this × ATR
    stop_atr_pad: float = 0.5
    long_only: bool = False


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


class RangeReversal:
    """Range-bound reversal at Bollinger Band extremes. Targets
    ~60% WR / 1.5:1 R:R — green zone via the high-win-rate corner."""

    name = "range_reversal"

    def __init__(self, params: RangeReversalParams = RangeReversalParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        warmup = max(p.bb_period, p.atr_period, p.rsi_period) + 2
        if len(candles) < warmup:
            return []

        upper, mid, lower = bollinger_bands(candles["close"], p.bb_period, p.bb_k)
        atr = atr_wilder(candles, p.atr_period)
        rsi = _rsi(candles["close"], p.rsi_period)
        ema_mid = ema(candles["close"], p.bb_period)
        # EMA slope across the last bb_period bars — used as range filter
        ema_prev = ema_mid.shift(p.bb_period)

        out: list[Signal] = []
        c = candles["close"].values
        o = candles["open"].values
        h = candles["high"].values
        l = candles["low"].values
        u_v = upper.values
        m_v = mid.values
        lo_v = lower.values
        atr_v = atr.values
        rsi_v = rsi.values
        slope_v = (ema_mid - ema_prev).values
        n = len(candles)

        for i in range(warmup, n):
            a = atr_v[i]
            if a <= 0:
                continue
            # Range filter — slope of EMA over the BB period must be small
            if pd.isna(slope_v[i]) or abs(slope_v[i]) > p.range_slope_atr * a:
                continue

            # ----- LONG: prior bar closed below lower band, this bar back inside -----
            if (i >= 1
                and c[i-1] < lo_v[i-1]
                and c[i] > lo_v[i]
                and c[i] > o[i]                    # bullish reversal
                and rsi_v[i] < p.rsi_oversold + 10):  # still oversold-ish
                stop = lo_v[i] - p.stop_atr_pad * a
                if stop <= 0 or stop >= c[i]:
                    continue
                risk = c[i] - stop
                if risk <= 0:
                    continue
                # Target = middle band — natural mean-reversion target
                target = m_v[i]
                if target <= c[i]:
                    continue
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=float(c[i]), stop_price=float(stop),
                    target_price=float(target),
                    reason="range_reversal_long",
                ))
                continue
            if p.long_only:
                continue

            # ----- SHORT: prior bar closed above upper band, this bar back inside -----
            if (i >= 1
                and c[i-1] > u_v[i-1]
                and c[i] < u_v[i]
                and c[i] < o[i]                    # bearish reversal
                and rsi_v[i] > p.rsi_overbought - 10):
                stop = u_v[i] + p.stop_atr_pad * a
                if stop <= c[i]:
                    continue
                risk = stop - c[i]
                if risk <= 0:
                    continue
                target = m_v[i]
                if target >= c[i] or target <= 0:
                    continue
                out.append(Signal(
                    bar_idx=i, direction="SHORT",
                    entry_price=float(c[i]), stop_price=float(stop),
                    target_price=float(target),
                    reason="range_reversal_short",
                ))

        return out
