"""
vol_breakout.py — Larry Williams-style volatility breakout.

Setup at bar i (today):
  range_prev    = high[i-1] - low[i-1]
  buy_trigger   = open[i] + k × range_prev
  sell_trigger  = open[i] - k × range_prev
  default k = 0.5

Entry semantics:
  If high[i] >= buy_trigger  AND  low[i]  >  sell_trigger  → LONG fill at buy_trigger.
  If low[i]  <= sell_trigger AND  high[i] <  buy_trigger  → SHORT fill at sell_trigger.
  If BOTH triggered intra-bar (very volatile bar): we conservatively skip — bar order
    of fills is ambiguous from H/L only.

Exit: stop = entry's opposite trigger; target = entry + target_R_mult × stop_distance.

Notes:
  - The Signal's entry_price is the trigger level, NOT the bar's close. The
    backtester uses signal.entry_price for PnL and floating mark-to-market —
    so signaling at the trigger price faithfully simulates a stop-entry order
    AS LONG AS we only fire when the bar actually traded through that level
    (verified intrabar via high/low). The bar's close vs. entry_price difference
    determines floating PnL on the entry bar — correct mark-to-market.
  - Documented in Larry Williams, "Long-Term Secrets to Short-Term Trading."
    Works on indices and high-volume futures; less reliable on FX.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.strategy import Signal


@dataclass(frozen=True)
class VolBreakoutParams:
    k: float = 0.5
    target_R_mult: float = 2.0
    long_only: bool = False


class VolBreakout:
    name = "vol_breakout"

    def __init__(self, params: VolBreakoutParams = VolBreakoutParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < 3:
            return []

        o = candles["open"].to_numpy()
        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()

        out: list[Signal] = []
        for i in range(1, n):
            rng_prev = h[i - 1] - l[i - 1]
            if rng_prev <= 0:
                continue
            buy_trig = float(o[i] + p.k * rng_prev)
            sell_trig = float(o[i] - p.k * rng_prev)
            hit_buy = h[i] >= buy_trig
            hit_sell = l[i] <= sell_trig
            if hit_buy and hit_sell:
                # Ambiguous order; skip (conservative).
                continue
            if hit_buy:
                entry = buy_trig
                stop = sell_trig   # opposite trigger
                risk = entry - stop
                if risk <= 0:
                    continue
                target = entry + p.target_R_mult * risk
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=entry, stop_price=stop,
                    target_price=target,
                    reason="vol_breakout_long_trigger_hit",
                ))
                continue
            if hit_sell and not p.long_only:
                entry = sell_trig
                stop = buy_trig
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
                    reason="vol_breakout_short_trigger_hit",
                ))
        return out
