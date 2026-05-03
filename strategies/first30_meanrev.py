"""
first30_meanrev.py — fade the first 30 minutes of the US session (M15 only).

The "first-30-min reversal" effect on US equity indices: the very first bars
of the regular trading session frequently overshoot in the direction of any
overnight gap or pre-market sentiment, then revert during the following hour.

Rule (M15 candles only):
  Bar A = the 13:30 UTC bar (first M15 of regular US session).
  Bar B = the 13:45 UTC bar (second M15 of regular US session).

  At the close of bar B:
    direction_strength = (close_A - open_A) / ATR(atr_period)
    if direction_strength > strength_atr_mult:
        SHORT at close_B  (fade the up-spike)
        stop = close_B + 1×ATR
        target = open_A   (gap-fill)
    if direction_strength < -strength_atr_mult:
        LONG at close_B   (fade the down-spike)
        stop = close_B - 1×ATR
        target = open_A   (gap-fill)

  Hard exit by 15:00 UTC ⇒ max_hold_bars = 5  (14:00, 14:15, 14:30, 14:45, 15:00).

INDEX-FOCUSED: the first-30-min reversal is documented for US equity indices,
NOT for FX where there's no clear "session open" with comparable retail flow.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.indicators import atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class First30MeanRevParams:
    session_open_utc_hour: int = 13
    session_open_utc_minute: int = 30
    bar_b_utc_hour: int = 13
    bar_b_utc_minute: int = 45
    atr_period: int = 14
    strength_atr_mult: float = 0.5     # bar A move must exceed this × ATR to qualify
    stop_atr_mult: float = 1.0
    max_hold_bars: int = 5             # 13:45 + 5×15min = 15:00
    long_only: bool = False


class First30MeanRev:
    name = "first30_meanrev"

    def __init__(self, params: First30MeanRevParams = First30MeanRevParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < p.atr_period + 2:
            return []

        if not pd.api.types.is_datetime64_any_dtype(candles["time"]):
            raise ValueError("candles['time'] must be datetime")
        t = pd.to_datetime(candles["time"])
        if t.dt.tz is None:
            t = t.dt.tz_localize("UTC")
        else:
            t = t.dt.tz_convert("UTC")

        o = candles["open"].to_numpy()
        c = candles["close"].to_numpy()
        atr = atr_wilder(candles, p.atr_period).to_numpy()
        hours = t.dt.hour.to_numpy()
        mins = t.dt.minute.to_numpy()

        out: list[Signal] = []
        for i in range(1, n):
            # bar B condition
            if not (hours[i] == p.bar_b_utc_hour and mins[i] == p.bar_b_utc_minute):
                continue
            # bar A is the bar immediately before
            j = i - 1
            if not (hours[j] == p.session_open_utc_hour
                    and mins[j] == p.session_open_utc_minute):
                continue
            a = atr[i]
            if a <= 0:
                continue
            move = c[j] - o[j]
            strength = move / a
            entry = float(c[i])
            target = float(o[j])   # gap-fill toward bar A's open

            if strength > p.strength_atr_mult:
                if p.long_only:
                    continue
                # SHORT (fade up move)
                stop = entry + p.stop_atr_mult * a
                if not (target < entry < stop):
                    continue
                out.append(Signal(
                    bar_idx=i, direction="SHORT",
                    entry_price=entry, stop_price=float(stop),
                    target_price=target,
                    reason="first30_fade_up_spike",
                    max_hold_bars=p.max_hold_bars,
                ))
            elif strength < -p.strength_atr_mult:
                # LONG (fade down move)
                stop = entry - p.stop_atr_mult * a
                if stop <= 0:
                    continue
                if not (stop < entry < target):
                    continue
                out.append(Signal(
                    bar_idx=i, direction="LONG",
                    entry_price=entry, stop_price=float(stop),
                    target_price=target,
                    reason="first30_fade_down_spike",
                    max_hold_bars=p.max_hold_bars,
                ))
        return out
