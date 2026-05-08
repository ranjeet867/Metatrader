"""
pivot_reaction_zone.py — fade first touch back to a recent pivot zone
with a reversal candle. The literal "support / resistance + last reaction"
strategy.

Setup:
  1. Identify recent pivot lows / highs (local extremes with pivot_window
     bars on each side, looking back zone_lookback bars).
  2. For each pivot low: define a "support zone" = [low, low + zone_pct]
     of the pivot's low. Mirror for highs.
  3. When price touches the zone (low[i] enters the band) AND prints a
     reversal candle (bullish: close > open AND lower wick > body), enter
     LONG at close[i] with stop below the zone bottom minus 0.5×ATR.
  4. Each zone is consumed after one signal (never re-fade the same zone).

Why this is interesting:
  - Your existing support_resistance.py uses pivot levels + a confirmation
    (RSI / EMA / volume / Bollinger). This is the simpler, candle-only
    version, which historically produces more trades and lower per-trade
    edge — making it a different node in the same family for diversification.
  - The "last reaction" angle: recent pivot levels (last ~50 bars) are more
    relevant than ancient ones because those are where current participants
    have memory of price action. zone_lookback caps the lookback.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class PivotReactionParams:
    pivot_window: int = 5
    zone_lookback: int = 50           # only pivots within last N bars count
    zone_pct: float = 0.0025          # zone band = ±0.25% of pivot price
    max_zones: int = 5                # keep last N pivots active per side
    require_reversal_candle: bool = True
    min_lower_wick_ratio: float = 1.0   # lower_wick / body for bullish
    atr_period: int = 14
    stop_atr_mult: float = 0.5        # buffer beyond zone edge
    target_R_mult: float = 2.0
    long_only: bool = False
    cooldown_bars: int = 10           # wait N bars after a fire on a zone


class PivotReactionZone:
    name = "pivot_reaction_zone"

    def __init__(self, params: PivotReactionParams = PivotReactionParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        warmup = max(p.pivot_window * 2 + p.zone_lookback,
                     p.atr_period * 3, 50)
        if n < warmup + 2:
            return []

        o = candles["open"].to_numpy()
        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()
        c = candles["close"].to_numpy()
        atr = atr_wilder(candles, p.atr_period).to_numpy()

        pw = p.pivot_window
        # Detect pivot lows and highs with strict-extreme rule
        is_low_pivot = np.zeros(n, dtype=bool)
        is_high_pivot = np.zeros(n, dtype=bool)
        for i in range(pw, n - pw):
            window_l = l[i - pw:i + pw + 1]
            if l[i] == window_l.min() and (window_l == l[i]).sum() == 1:
                is_low_pivot[i] = True
            window_h = h[i - pw:i + pw + 1]
            if h[i] == window_h.max() and (window_h == h[i]).sum() == 1:
                is_high_pivot[i] = True

        # Walk forward; at each bar i, build the active-zones set and
        # check for touch-and-reversal.
        out: list[Signal] = []
        last_fire_bar_per_zone: dict[int, int] = {}
        for i in range(warmup, n):
            atr_i = atr[i]
            if np.isnan(atr_i) or atr_i <= 0:
                continue
            # Active pivot lows: pivot bars in (i - zone_lookback, i - pw]
            # — must be at least pw bars old so the pivot is confirmed.
            lo_bound = max(0, i - p.zone_lookback)
            hi_bound = i - pw
            if hi_bound <= lo_bound:
                continue
            active_low_pivots = [j for j in range(hi_bound, lo_bound - 1, -1)
                                  if is_low_pivot[j]][:p.max_zones]
            active_high_pivots = [j for j in range(hi_bound, lo_bound - 1, -1)
                                  if is_high_pivot[j]][:p.max_zones]

            # -- LONG: support touch + bullish reversal candle --
            for piv_j in active_low_pivots:
                last_fire = last_fire_bar_per_zone.get(piv_j, -10**9)
                if i - last_fire < p.cooldown_bars:
                    continue
                zone_top = l[piv_j] * (1 + p.zone_pct)
                zone_bot = l[piv_j] * (1 - p.zone_pct)
                touched = (l[i] <= zone_top) and (l[i] >= zone_bot * 0.98)
                if not touched:
                    continue
                # Reversal candle: green + lower wick >= body
                body = abs(c[i] - o[i])
                lower_wick = min(o[i], c[i]) - l[i]
                bullish = c[i] > o[i]
                wick_ok = (
                    not p.require_reversal_candle
                    or (bullish and body > 0
                        and lower_wick / body >= p.min_lower_wick_ratio)
                )
                if not wick_ok:
                    continue
                entry = float(c[i])
                stop = float(zone_bot) - p.stop_atr_mult * float(atr_i)
                risk = entry - stop
                if risk <= 0:
                    continue
                target = entry + p.target_R_mult * risk
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=entry, stop_price=stop,
                    target_price=target,
                    reason=f"support_zone_touch_pivot{piv_j}",
                ))
                last_fire_bar_per_zone[piv_j] = i
                break    # one signal per bar

            # -- SHORT: resistance touch + bearish reversal candle --
            if p.long_only:
                continue
            for piv_j in active_high_pivots:
                last_fire = last_fire_bar_per_zone.get(piv_j, -10**9)
                if i - last_fire < p.cooldown_bars:
                    continue
                zone_bot_h = h[piv_j] * (1 - p.zone_pct)
                zone_top_h = h[piv_j] * (1 + p.zone_pct)
                touched = (h[i] >= zone_bot_h) and (h[i] <= zone_top_h * 1.02)
                if not touched:
                    continue
                body = abs(c[i] - o[i])
                upper_wick = h[i] - max(o[i], c[i])
                bearish = c[i] < o[i]
                wick_ok = (
                    not p.require_reversal_candle
                    or (bearish and body > 0
                        and upper_wick / body >= p.min_lower_wick_ratio)
                )
                if not wick_ok:
                    continue
                entry = float(c[i])
                stop = float(zone_top_h) + p.stop_atr_mult * float(atr_i)
                risk = stop - entry
                if risk <= 0:
                    continue
                target = entry - p.target_R_mult * risk
                if target <= 0:
                    continue
                out.append(Signal(
                    bar_idx=i, direction="SHORT",
                    entry_price=entry, stop_price=stop,
                    target_price=target,
                    reason=f"resistance_zone_touch_pivot{piv_j}",
                ))
                last_fire_bar_per_zone[piv_j] = i
                break
        return out
