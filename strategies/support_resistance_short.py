"""
support_resistance_short.py — bearish mirror of support_resistance.

Setup (SHORT — fade pivot resistance):
  1. Detect resistance levels = pivot highs confirmed by `pivot_lookback`
     bars on each side. Keep last `keep_levels` rolling.
  2. Drop levels that have been broken (close > 0.3% past the level).
  3. Bar high tags within `touch_pct` of any active resistance level.
  4. Close < open (bearish rejection candle).
  5. Variant filter (mirror of long-side):
       plain      — no extra check
       rsi        — RSI(14) > rsi_overbought (default 65)
       ema_filter — close < EMA(ema_trend_period) (only fade in downtrend)
       volume     — touch bar volume > vol_mult × 20-bar average
       bb_upper   — bar high pierces upper Bollinger band

Stop = touched_level + stop_atr_pad × ATR(14)
Target = entry − rr_target × stop_distance
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder, rsi_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class SupportResistanceShortParams:
    variant: str = "plain"
    pivot_lookback: int = 10
    keep_levels: int = 5
    touch_pct: float = 0.003
    level_break_pct: float = 0.003
    stop_atr_pad: float = 0.5
    atr_period: int = 14
    rr_target: float = 2.0
    rsi_overbought: float = 65.0
    rsi_period: int = 14
    ema_trend_period: int = 200
    vol_mult: float = 1.5
    vol_avg_window: int = 20
    bb_period: int = 20
    bb_k: float = 2.0
    long_only: bool = False             # always False; this is SHORT-only


def _find_pivot_highs(highs: np.ndarray, k: int) -> list[int]:
    out: list[int] = []
    for i in range(k, len(highs) - k):
        window = highs[i - k:i + k + 1]
        if highs[i] == window.max() and (window == highs[i]).sum() == 1:
            out.append(i)
    return out


class SupportResistanceShort:
    name = "support_resistance_short"

    def __init__(self, params: SupportResistanceShortParams
                 = SupportResistanceShortParams()):
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
        if p.variant == "bb_upper":
            s = pd.Series(closes)
            bb_mid = s.rolling(p.bb_period).mean().values
            bb_std = s.rolling(p.bb_period).std().values
            bb_hi = bb_mid + p.bb_k * bb_std
        else:
            bb_hi = None

        pivots = _find_pivot_highs(highs, p.pivot_lookback)
        active_levels: list[float] = []
        pivot_ptr = 0

        out: list[Signal] = []
        for i in range(250, n):
            while (pivot_ptr < len(pivots)
                   and pivots[pivot_ptr] + p.pivot_lookback <= i):
                lvl = float(highs[pivots[pivot_ptr]])
                active_levels.append(lvl)
                if len(active_levels) > p.keep_levels:
                    active_levels.pop(0)
                pivot_ptr += 1
            # Prune broken-through levels (close > level by margin)
            active_levels = [
                lv for lv in active_levels
                if closes[i - 1] < lv * (1 + p.level_break_pct)
            ]
            if not active_levels or atr[i] <= 0:
                continue

            touched = None
            for lv in active_levels:
                if (abs(highs[i] - lv) / lv < p.touch_pct
                        or (lows[i] <= lv <= highs[i])):
                    touched = lv
                    break
            if touched is None:
                continue
            if not (closes[i] < opens[i]):
                continue

            if p.variant == "rsi":
                if not (rsi[i] > p.rsi_overbought):
                    continue
            elif p.variant == "ema_filter":
                if not (closes[i] < e_trend[i]):
                    continue
            elif p.variant == "volume":
                avg = vol_avg[i] if vol_avg[i] and not np.isnan(
                    vol_avg[i]) else 1.0
                if not (vol[i] > p.vol_mult * avg):
                    continue
            elif p.variant == "bb_upper":
                if np.isnan(bb_hi[i]):
                    continue
                if not (highs[i] >= bb_hi[i]):
                    continue

            stop = touched + p.stop_atr_pad * atr[i]
            if stop <= closes[i]:
                continue
            risk = stop - closes[i]
            target = closes[i] - p.rr_target * risk
            if target <= 0:
                continue
            out.append(Signal(
                bar_idx=i, direction="SHORT",
                entry_price=float(closes[i]),
                stop_price=float(stop),
                target_price=float(target),
                reason=f"sr_short_{p.variant}_rejection",
            ))
        return out
