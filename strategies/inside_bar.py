"""
inside_bar.py — inside-bar breakout (compression-then-expansion).

An inside bar is a bar whose entire range is contained within the prior
bar's range:
    high[i] <= high[i-1]   AND   low[i] >= low[i-1]

The "mother bar" is bar i-1. The pattern represents short-term volatility
contraction; a break out of the mother bar's range often resolves with
follow-through.

Rule:
  When bar i is an inside bar, bar i+1 is the SETUP bar.
  At bar i+1's CLOSE:
    if close > inside_bar_high (= high[i])  → LONG, stop at inside_bar_low (= low[i])
    if close < inside_bar_low  (= low[i])   → SHORT (or skip if long_only)
                                              stop at inside_bar_high
  target = entry ± target_R_mult × stop_distance.

Notes:
  - We use INSIDE BAR (i) high/low for stop, not the mother bar (i-1).
    This is the more aggressive/standard version of the rule and produces
    closer stops. Some literature uses the mother bar — that's a parameter
    we could expose later if needed.
  - Each inside bar produces at most one signal at the very next bar.
  - Works on H1, D1; M15 inside bars are too noisy to rely on.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.strategy import Signal


@dataclass(frozen=True)
class InsideBarParams:
    target_R_mult: float = 2.0
    long_only: bool = False
    use_mother_bar_stop: bool = False    # if True, stop = mother bar's low/high


class InsideBar:
    name = "inside_bar"

    def __init__(self, params: InsideBarParams = InsideBarParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < 3:
            return []

        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()
        c = candles["close"].to_numpy()

        out: list[Signal] = []
        for i in range(1, n - 1):
            # bar i is the inside bar candidate; bar i-1 is the mother
            if h[i] > h[i - 1] or l[i] < l[i - 1]:
                continue
            # Strict inside: must be inside, not equal on both sides
            if h[i] == h[i - 1] and l[i] == l[i - 1]:
                continue
            inside_high = float(h[i])
            inside_low = float(l[i])
            if p.use_mother_bar_stop:
                stop_long_level = float(l[i - 1])
                stop_short_level = float(h[i - 1])
            else:
                stop_long_level = inside_low
                stop_short_level = inside_high

            j = i + 1
            entry = float(c[j])
            if entry > inside_high:
                stop = stop_long_level
                risk = entry - stop
                if risk <= 0:
                    continue
                target = entry + p.target_R_mult * risk
                out.append(Signal(
                    bar_idx=j, direction="LONG",
                    entry_price=entry, stop_price=stop,
                    target_price=target,
                    reason="inside_bar_break_up",
                ))
                continue
            if p.long_only:
                continue
            if entry < inside_low:
                stop = stop_short_level
                risk = stop - entry
                if risk <= 0:
                    continue
                target = entry - p.target_R_mult * risk
                if target <= 0:
                    continue
                out.append(Signal(
                    bar_idx=j, direction="SHORT",
                    entry_price=entry, stop_price=stop,
                    target_price=target,
                    reason="inside_bar_break_down",
                ))
        return out
