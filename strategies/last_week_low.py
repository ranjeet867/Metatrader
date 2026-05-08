"""
last_week_low.py — mean-reversion at prior-week's low.

Setup (LONG only — this is mean-reversion at support):
  1. Compute prior COMPLETED week's low (Mon-Fri, ISO week)
  2. Bar's low touches within 0.5% of prior_week_low (a "test")
  3. Bar closes higher than open (rejection candle)
  4. (Optional) 50-EMA > 200-EMA on D1  (regime filter)

Stop = prior_week_low − 0.5 × ATR(14)
Target = entry + rr_target × stop_distance  (R:R 1:1 → 1:4 supported)

Empirically best fixed-R:R cells (see optimization_2026-05-06):
  - JP225.cash D1, R:R 1:2  →  PF 2.37, recovers in 1d
  - US500.cash D1, R:R 1:4  →  PF 2.12, 1.2% DD
  - XAGUSD D1, R:R 1:4    →  PF 1.80 (with 16.8% DD — sat-only)

This module ships the FIXED R:R variants. The "trail until close < prior
week low" exit (the very best XAGUSD cell at PF 4.63) is implemented in
scripts/sweep_last_week_low.py and should be added as a separate
trail-aware strategy when the backtester gains trailing-exit support.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class LastWeekLowParams:
    touch_tolerance_pct: float = 0.005    # within 0.5% of prior week low
    stop_atr_pad: float = 0.5             # stop = pwl - this × ATR
    rr_target: float = 2.0                # 1:1, 1:2, 1:3, 1:4
    use_regime_filter: bool = True        # 50-EMA > 200-EMA on D1
    atr_period: int = 14
    long_only: bool = True                # mean-rev at low → long only


def _prior_week_lows(df: pd.DataFrame) -> np.ndarray:
    """For each D1 bar return the prior completed ISO-week's low.

    Uses ISO week numbers so Mon-Fri rolls up consistently.
    """
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    iso = df["time"].dt.isocalendar()
    df["wk"] = iso.year.astype(str) + "-W" + iso.week.astype(str).str.zfill(2)
    weekly_lows = df.groupby("wk")["low"].min()
    week_order = list(weekly_lows.index)
    week_to_prior = {}
    for k, this_wk in enumerate(week_order):
        if k == 0:
            week_to_prior[this_wk] = np.nan
        else:
            week_to_prior[this_wk] = float(weekly_lows.iloc[k - 1])
    return df["wk"].map(week_to_prior).to_numpy()


class LastWeekLow:
    name = "last_week_low"

    def __init__(self, params: LastWeekLowParams = LastWeekLowParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < 220:
            return []
        pwl = _prior_week_lows(candles)
        atr = atr_wilder(candles, p.atr_period).values
        c = candles["close"].values
        o = candles["open"].values
        l = candles["low"].values

        if p.use_regime_filter:
            e50 = ema(candles["close"], 50).values
            e200 = ema(candles["close"], 200).values
        else:
            e50 = e200 = None

        out: list[Signal] = []
        for i in range(200, n):
            level = pwl[i]
            if not np.isfinite(level) or atr[i] <= 0:
                continue
            if p.use_regime_filter and not (e50[i] > e200[i]):
                continue
            # 1) low touches prior-week-low within tolerance
            if abs(l[i] - level) / level > p.touch_tolerance_pct:
                continue
            # 2) bullish rejection candle
            if not (c[i] > o[i]):
                continue
            stop = level - p.stop_atr_pad * atr[i]
            if stop <= 0 or stop >= c[i]:
                continue
            risk = c[i] - stop
            target = c[i] + p.rr_target * risk
            out.append(Signal(
                bar_idx=i, direction="LONG",
                entry_price=float(c[i]),
                stop_price=float(stop),
                target_price=float(target),
                reason="last_week_low_test",
            ))
        return out
