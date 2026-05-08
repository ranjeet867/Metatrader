"""
vol_break.py — Donchian breakout filtered by realised-volatility expansion.

Mechanism (institutional-grade):
A vanilla N-period high-break tells you nothing about WHY price is breaking
out. In compressed-volatility regimes most breaks fail (mean-reversion dominates
when realised σ is low); in expanding-vol regimes breaks have continuation
because something has changed in the order book.

vol_break adds ONE filter to donchian_20:
  realised_vol(window=10) > vol_mult × realised_vol(window=50)

Realised vol = standard deviation of log returns, annualisation-free
(comparison is ratio-based so units cancel).

Defaults (anti-curve-fit choices):
- period=20: same as donchian_20 baseline so we can A/B compare
- vol_mult=1.5: documented threshold in academic literature (Cooper & Schindler);
  not tuned per-ticker. Higher = fewer trades but sharper edge.
- short_window=10, long_window=50: round numbers, half-decade ratio.

Same SL/TP as donchian_breakout so the two are directly comparable.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import atr_wilder, rolling_high, rolling_low
from core.strategy import Signal


@dataclass(frozen=True)
class VolBreakParams:
    period: int = 20
    short_vol_window: int = 10
    long_vol_window: int = 50
    vol_mult: float = 1.5
    atr_period: int = 14
    stop_atr_mult: float = 1.5
    target_atr_mult: float = 3.0
    long_only: bool = False


class VolBreak:
    name = "vol_break"

    def __init__(self, params: VolBreakParams = VolBreakParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        warmup = max(p.period, p.atr_period, p.long_vol_window) + 2
        if len(candles) < warmup:
            return []

        prior_hi = rolling_high(candles["close"], p.period).shift(1)
        prior_lo = rolling_low(candles["close"], p.period).shift(1)
        atr = atr_wilder(candles, p.atr_period)

        # Realised volatility = stdev of log returns. Ratio is unitless so
        # works across all price scales (FX, indices, gold).
        log_ret = np.log(candles["close"] / candles["close"].shift(1))
        rv_short = log_ret.rolling(p.short_vol_window).std()
        rv_long = log_ret.rolling(p.long_vol_window).std()
        vol_ratio = (rv_short / rv_long).values

        out: list[Signal] = []
        c = candles["close"].values
        ph = prior_hi.values
        pl = prior_lo.values
        atr_v = atr.values
        n = len(candles)

        for i in range(1, n):
            a = atr_v[i]
            if a <= 0 or pd.isna(ph[i]) or pd.isna(pl[i]):
                continue
            # CORE FILTER — only take breakouts when realised vol is expanding
            vr = vol_ratio[i]
            if pd.isna(vr) or vr < p.vol_mult:
                continue
            # Cross above (long break)
            if c[i] > ph[i] and c[i - 1] <= (
                ph[i - 1] if not pd.isna(ph[i - 1]) else c[i] + 1
            ):
                stop = c[i] - p.stop_atr_mult * a
                target = c[i] + p.target_atr_mult * a
                if stop > 0:
                    out.append(Signal(
                        bar_idx=i, direction="LONG",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason=f"vol_break_long_vr={vr:.2f}",
                    ))
                continue
            if p.long_only:
                continue
            # Cross below (short break)
            if c[i] < pl[i] and c[i - 1] >= (
                pl[i - 1] if not pd.isna(pl[i - 1]) else c[i] - 1
            ):
                stop = c[i] + p.stop_atr_mult * a
                target = c[i] - p.target_atr_mult * a
                if target > 0:
                    out.append(Signal(
                        bar_idx=i, direction="SHORT",
                        entry_price=float(c[i]), stop_price=float(stop),
                        target_price=float(target),
                        reason=f"vol_break_short_vr={vr:.2f}",
                    ))
        return out
