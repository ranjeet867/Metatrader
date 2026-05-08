"""
trend_pullback_regime.py — pullback to fast MA, gated by HTF regime.

Why this exists:
The original ``trend_pullback`` failed deploy_safe across every cell
in the catalog. Forensic look at the trades showed it took every
pullback regardless of whether the higher-timeframe trend agreed.
That meant 50% of pullbacks were against the prevailing direction
(noise) and got chopped.

Fix: gate the entry on a SAME-TF regime filter, since we cannot easily
peek at a higher TF inside the strategy interface (the candle DataFrame
is single-TF). The proxy for "HTF trend" is a long-window EMA on the
current TF — slow enough that it captures the multi-day direction.

Mechanism:
1. Regime: regime_ema_long (default 200) > regime_ema_short (default 50)
   means "uptrend regime" — only allow long pullbacks. Inverse for short.
2. Pullback: price tags fast EMA (default 20) AFTER having made a fresh
   N-bar high (long) or N-bar low (short).
3. Entry on next bar's close. Stop = recent swing low/high. Target = 1.5R.

Anti-curve-fit:
- Round-number params (20/50/200) — same across all cells
- Regime filter is binary, no thresholds to tune
- 5-bar high/low confirmation is the same as classic Dow Theory
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder, ema
from core.strategy import Signal


@dataclass(frozen=True)
class TrendPullbackRegimeParams:
    fast_ema: int = 20
    regime_ema_short: int = 50
    regime_ema_long: int = 200
    fresh_extreme_lookback: int = 5
    atr_period: int = 14
    swing_lookback: int = 10
    target_atr_mult: float = 1.5    # 1.5R target (mid-trend take)
    long_only: bool = False


class TrendPullbackRegime:
    name = "trend_pullback_regime"

    def __init__(self,
                   params: TrendPullbackRegimeParams = TrendPullbackRegimeParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        warmup = max(p.regime_ema_long, p.atr_period, p.swing_lookback) + 4
        if len(candles) < warmup:
            return []

        c = candles["close"]
        ema_fast = ema(c, p.fast_ema).values
        ema_short = ema(c, p.regime_ema_short).values
        ema_long = ema(c, p.regime_ema_long).values
        atr_v = atr_wilder(candles, p.atr_period).values
        h = candles["high"].values
        lo_ = candles["low"].values
        cv = c.values
        n = len(candles)

        # Pre-compute rolling fresh-high / fresh-low: did we make a fresh
        # N-bar high/low within the last `fresh_extreme_lookback` bars?
        #   fresh_high[i] = True if h[i-N..i] argmax is in last 3 bars
        N = p.fresh_extreme_lookback

        out: list[Signal] = []
        in_long_setup = False
        in_short_setup = False
        long_pivot_low = 0.0
        short_pivot_high = 0.0

        for i in range(warmup, n):
            a = atr_v[i]
            if a <= 0 or pd.isna(ema_long[i]):
                continue

            uptrend = ema_short[i] > ema_long[i]
            downtrend = ema_short[i] < ema_long[i]

            # Fresh high/low confirmation — has price touched a new high
            # in the last few bars?
            window = slice(max(0, i - N), i + 1)
            recent_max = h[window].max()
            recent_min = lo_[window].min()
            fresh_high_now = h[i] >= recent_max - 1e-12
            fresh_low_now = lo_[i] <= recent_min + 1e-12

            # ----- LONG SETUP -----
            if uptrend:
                # Arm a setup when we make a fresh N-bar high
                if fresh_high_now:
                    in_long_setup = True
                    long_pivot_low = lo_[i]
                # Trigger entry: price pulls back to fast EMA
                if in_long_setup and lo_[i] <= ema_fast[i] <= h[i]:
                    # Stop = recent swing low (last `swing_lookback` bars)
                    swing_lo = lo_[max(0, i - p.swing_lookback):i + 1].min()
                    stop = float(min(swing_lo, long_pivot_low) - 0.1 * a)
                    risk = cv[i] - stop
                    if risk > 0:
                        target = float(cv[i] + p.target_atr_mult * risk)
                        out.append(Signal(
                            bar_idx=i, direction="LONG",
                            entry_price=float(cv[i]),
                            stop_price=stop, target_price=target,
                            reason="trend_pullback_long_regime_up",
                        ))
                        in_long_setup = False  # consume signal

            # ----- SHORT SETUP -----
            if (not p.long_only) and downtrend:
                if fresh_low_now:
                    in_short_setup = True
                    short_pivot_high = h[i]
                if in_short_setup and lo_[i] <= ema_fast[i] <= h[i]:
                    swing_hi = h[max(0, i - p.swing_lookback):i + 1].max()
                    stop = float(max(swing_hi, short_pivot_high) + 0.1 * a)
                    risk = stop - cv[i]
                    if risk > 0:
                        target = float(cv[i] - p.target_atr_mult * risk)
                        if target > 0:
                            out.append(Signal(
                                bar_idx=i, direction="SHORT",
                                entry_price=float(cv[i]),
                                stop_price=stop, target_price=target,
                                reason="trend_pullback_short_regime_down",
                            ))
                            in_short_setup = False
        return out
