"""
last_week_high.py — bearish mirror of last_week_low. SHORT-only.

Setup (SHORT — fade prior-week resistance):
  1. Compute prior COMPLETED week's high (Mon-Fri, ISO week)
  2. Bar's high tags within `touch_tolerance_pct` of prior_week_high
  3. Bar closes lower than open (bearish rejection candle)
  4. (Optional) 50-EMA < 200-EMA on D1  (regime filter — only fade in
     downtrend; flip to >0 if you want to fade rallies in any regime)

Stop = prior_week_high + stop_atr_pad × ATR(14)
Target = entry − rr_target × stop_distance  (R:R 1:1, 1:2, 1:3, 1:4)

This strategy is the bearish complement to `last_week_low`. Use it
when you want explicit short exposure on tickers that respect weekly
resistance (works well on JP225, US500, metals during pullbacks).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class LastWeekHighParams:
    touch_tolerance_pct: float = 0.005   # within 0.5% of prior week high
    stop_atr_pad: float = 0.5            # stop = pwh + this × ATR
    rr_target: float = 2.0               # 1:1 → 1:4 supported
    use_regime_filter: bool = True        # 50-EMA < 200-EMA on D1
    atr_period: int = 14
    long_only: bool = False              # always False here; this is SHORT-only


def _prior_week_highs(df: pd.DataFrame) -> np.ndarray:
    """For each D1 bar return the prior completed ISO-week's high."""
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    iso = df["time"].dt.isocalendar()
    df["wk"] = iso.year.astype(str) + "-W" + iso.week.astype(str).str.zfill(2)
    weekly_highs = df.groupby("wk")["high"].max()
    week_order = list(weekly_highs.index)
    week_to_prior: dict[str, float] = {}
    for k, this_wk in enumerate(week_order):
        if k == 0:
            week_to_prior[this_wk] = float("nan")
        else:
            week_to_prior[this_wk] = float(weekly_highs.iloc[k - 1])
    return df["wk"].map(week_to_prior).to_numpy()


class LastWeekHigh:
    name = "last_week_high"

    def __init__(self, params: LastWeekHighParams = LastWeekHighParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < 220:
            return []
        pwh = _prior_week_highs(candles)
        atr = atr_wilder(candles, p.atr_period).values
        c = candles["close"].values
        o = candles["open"].values
        h = candles["high"].values

        if p.use_regime_filter:
            e50 = ema(candles["close"], 50).values
            e200 = ema(candles["close"], 200).values
        else:
            e50 = e200 = None

        out: list[Signal] = []
        for i in range(200, n):
            level = pwh[i]
            if not np.isfinite(level) or atr[i] <= 0:
                continue
            if p.use_regime_filter and not (e50[i] < e200[i]):
                continue
            # 1) high tags prior-week-high within tolerance
            if abs(h[i] - level) / level > p.touch_tolerance_pct:
                continue
            # 2) bearish rejection candle
            if not (c[i] < o[i]):
                continue
            stop = level + p.stop_atr_pad * atr[i]
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
                reason="last_week_high_test",
            ))
        return out
