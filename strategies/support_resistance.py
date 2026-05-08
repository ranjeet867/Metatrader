"""
support_resistance.py — pivot-based support bounce with optional
confirmation filter (plain / RSI / EMA / volume / BB-lower).

Setup (LONG only — bounce off support):
  1. Detect support levels = pivot lows confirmed by `pivot_lookback`
     bars on each side. Keep the last `keep_levels` rolling.
  2. Drop levels that have been broken (close > 0.3% past the level).
  3. Bar low touches within `touch_pct` of any active support level.
  4. Close > open (rejection candle).
  5. Confirmation filter (per `variant`):
       plain      — no extra check
       rsi        — RSI(14) < `rsi_oversold` (default 35)
       ema_filter — close > EMA(`ema_trend_period`) (only fade against trend)
       volume     — touch bar volume > `vol_mult` × 20-bar average
       bb_lower   — bar low pierces lower Bollinger band (vol-confirmed)

Stop = touched_level − stop_atr_pad × ATR(14)
Target = entry + rr_target × stop_distance

Empirically best cells (sweep_support_resistance.py, 2026-05-06):
  - plain XAUUSD D1 R:R 1:2 → PF 3.17, mean_R +0.66, DD 3.5%
  - plain XPTUSD D1 R:R 1:2 → PF 3.27, mean_R +0.63, DD 1.6%
  - ema_filter XPTUSD H1   → PF 1.66, mean_R +0.29, DD 3.8%
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder, rsi_wilder
from core.strategy import Signal


Variant = Literal["plain", "rsi", "ema_filter", "volume", "bb_lower"]


@dataclass(frozen=True)
class SupportResistanceParams:
    variant: str = "plain"            # plain / rsi / ema_filter / volume / bb_lower
    pivot_lookback: int = 10           # bars on each side of pivot
    keep_levels: int = 5               # rolling memory of last N levels
    touch_pct: float = 0.003           # within 0.3% of level
    level_break_pct: float = 0.003     # close past by > 0.3% invalidates
    stop_atr_pad: float = 0.5
    atr_period: int = 14
    rr_target: float = 2.0
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0       # used by SHORT side of "rsi" variant
    rsi_period: int = 14
    ema_trend_period: int = 200
    vol_mult: float = 1.5
    vol_avg_window: int = 20
    bb_period: int = 20
    bb_k: float = 2.0
    long_only: bool = True             # if True → only LONG bounces from support
    short_only: bool = False           # if True → only SHORT rejections at resistance
    # When BOTH long_only=False AND short_only=False → bidir (LONG+SHORT)


def _find_pivot_lows(lows: np.ndarray, k: int) -> list[int]:
    out: list[int] = []
    for i in range(k, len(lows) - k):
        window = lows[i - k:i + k + 1]
        if lows[i] == window.min() and (window == lows[i]).sum() == 1:
            out.append(i)
    return out


def _find_pivot_highs(highs: np.ndarray, k: int) -> list[int]:
    """Mirror of pivot lows — confirmed resistance level when bar is the
    highest of `k` bars on each side."""
    out: list[int] = []
    for i in range(k, len(highs) - k):
        window = highs[i - k:i + k + 1]
        if highs[i] == window.max() and (window == highs[i]).sum() == 1:
            out.append(i)
    return out


class SupportResistance:
    name = "support_resistance"

    def __init__(self, params: SupportResistanceParams
                 = SupportResistanceParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < 250:
            return []
        closes = candles["close"].values
        highs = candles["high"].values
        lows = candles["low"].values
        opens = candles["open"].values
        atr = atr_wilder(candles, p.atr_period).values

        if p.variant == "rsi":
            rsi = rsi_wilder(candles["close"], p.rsi_period).values
        else:
            rsi = None
        if p.variant == "ema_filter":
            e_trend = ema(candles["close"], p.ema_trend_period).values
        else:
            e_trend = None
        if p.variant == "volume":
            if "tick_volume" in candles.columns:
                vol = candles["tick_volume"].values.astype(float)
            elif "volume" in candles.columns:
                vol = candles["volume"].values.astype(float)
            else:
                vol = (candles["high"] - candles["low"]
                        ).values.astype(float)
            vol_avg = (pd.Series(vol)
                       .rolling(p.vol_avg_window).mean().values)
        else:
            vol = vol_avg = None
        if p.variant == "bb_lower":
            s = pd.Series(closes)
            bb_mid = s.rolling(p.bb_period).mean().values
            bb_std = s.rolling(p.bb_period).std().values
            bb_lo = bb_mid - p.bb_k * bb_std
        else:
            bb_lo = None

        pivots = _find_pivot_lows(lows, p.pivot_lookback)
        active_levels: list[float] = []
        pivot_ptr = 0

        out: list[Signal] = []
        for i in range(250, n):
            # Promote newly-confirmed pivots
            while (pivot_ptr < len(pivots)
                   and pivots[pivot_ptr] + p.pivot_lookback <= i):
                lvl = float(lows[pivots[pivot_ptr]])
                active_levels.append(lvl)
                if len(active_levels) > p.keep_levels:
                    active_levels.pop(0)
                pivot_ptr += 1
            # Prune broken levels
            active_levels = [
                lv for lv in active_levels
                if closes[i - 1] > lv * (1 - p.level_break_pct)
            ]
            if not active_levels or atr[i] <= 0:
                continue

            # Find touched level
            touched = None
            for lv in active_levels:
                if (abs(lows[i] - lv) / lv < p.touch_pct
                        or (lows[i] <= lv <= highs[i])):
                    touched = lv
                    break
            if touched is None:
                continue
            if not (closes[i] > opens[i]):
                continue

            # Variant filter
            if p.variant == "rsi":
                if not (rsi[i] < p.rsi_oversold):
                    continue
            elif p.variant == "ema_filter":
                if not (closes[i] > e_trend[i]):
                    continue
            elif p.variant == "volume":
                avg = vol_avg[i] if vol_avg[i] and not np.isnan(
                    vol_avg[i]) else 1.0
                if not (vol[i] > p.vol_mult * avg):
                    continue
            elif p.variant == "bb_lower":
                if np.isnan(bb_lo[i]):
                    continue
                if not (lows[i] <= bb_lo[i]):
                    continue

            stop = touched - p.stop_atr_pad * atr[i]
            if stop <= 0 or stop >= closes[i]:
                continue
            risk = closes[i] - stop
            target = closes[i] + p.rr_target * risk
            out.append(Signal(
                bar_idx=i, direction="LONG",
                entry_price=float(closes[i]),
                stop_price=float(stop),
                target_price=float(target),
                reason=f"sr_{p.variant}_bounce",
            ))
        return out
