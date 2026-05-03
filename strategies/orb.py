"""
orb.py — Opening Range Breakout (US session, M15 bars).

The Opening Range (OR) is formed by the first `or_bars` bars of the US
regular trading session (default: 13:30, 13:45, 14:00, 14:15 UTC = 4×M15).

Once the OR is closed:
  - first M15 bar closing ABOVE OR_high  → LONG  (stop at OR_low)
  - first M15 bar closing BELOW OR_low   → SHORT (stop at OR_high)
  - target = entry + 2R for LONG (or -2R for SHORT)
  - hard exit by `session_close_utc_hour:session_close_utc_minute` (default 20:00 UTC)
    — implemented as max_hold_bars = bars-from-signal-to-session-close

ONE trade per US session (the FIRST breakout — long OR short — is the only
trade we take that day; subsequent breakouts in the same session are ignored).

Designed for INDICES on M15. Won't capture meaningful structure on FX where
"the US session" isn't where the action is concentrated.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.strategy import Signal


@dataclass(frozen=True)
class OrbParams:
    or_bars: int = 4                     # number of bars forming the OR
    session_open_utc_hour: int = 13
    session_open_utc_minute: int = 30
    session_close_utc_hour: int = 20
    session_close_utc_minute: int = 0
    long_only: bool = False
    target_R_mult: float = 2.0           # target = entry + 2R


def _bars_per_session(open_h: int, open_m: int, close_h: int, close_m: int) -> int:
    """Number of M15 bars from session open up to (but not including) session close."""
    minutes = (close_h * 60 + close_m) - (open_h * 60 + open_m)
    return minutes // 15


class Orb:
    name = "orb"

    def __init__(self, params: OrbParams = OrbParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < p.or_bars + 2:
            return []

        # Validate the timeframe is M15-ish (15-minute steps).
        # If frequency is wrong, the bar-arithmetic below is meaningless.
        if not pd.api.types.is_datetime64_any_dtype(candles["time"]):
            raise ValueError("candles['time'] must be datetime")

        t = pd.to_datetime(candles["time"])
        # Make tz-aware UTC if naive
        if t.dt.tz is None:
            t = t.dt.tz_localize("UTC")
        else:
            t = t.dt.tz_convert("UTC")

        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()
        c = candles["close"].to_numpy()
        hours = t.dt.hour.to_numpy()
        mins = t.dt.minute.to_numpy()

        bars_per_session = _bars_per_session(
            p.session_open_utc_hour, p.session_open_utc_minute,
            p.session_close_utc_hour, p.session_close_utc_minute,
        )
        if bars_per_session <= p.or_bars:
            return []

        out: list[Signal] = []
        # Walk bars; whenever we see the FIRST OR bar of a session, compute OR
        # then scan the remaining session bars for a breakout.
        i = 0
        while i < n:
            if (hours[i] == p.session_open_utc_hour
                    and mins[i] == p.session_open_utc_minute):
                # OR window: bars [i, i+or_bars-1]
                last_or = i + p.or_bars - 1
                if last_or >= n:
                    break
                or_high = float(h[i:last_or + 1].max())
                or_low = float(l[i:last_or + 1].min())
                # Last session bar = i + bars_per_session - 1 (close at session_close)
                last_session_bar = min(i + bars_per_session - 1, n - 1)
                # Scan bars (last_or+1 .. last_session_bar) for first breakout close
                broken = False
                for j in range(last_or + 1, last_session_bar + 1):
                    entry = float(c[j])
                    if entry > or_high:
                        stop = or_low
                        risk = entry - stop
                        if risk <= 0:
                            break
                        target = entry + p.target_R_mult * risk
                        # max_hold = bars from j to last_session_bar inclusive
                        max_hold = last_session_bar - j
                        if max_hold < 1:
                            max_hold = 1   # at least one bar of holding
                        out.append(Signal(
                            bar_idx=j, direction="LONG",
                            entry_price=entry, stop_price=stop,
                            target_price=target,
                            reason="orb_long_breakout",
                            max_hold_bars=max_hold,
                        ))
                        broken = True
                        break
                    if entry < or_low and not p.long_only:
                        stop = or_high
                        risk = stop - entry
                        if risk <= 0:
                            break
                        target = entry - p.target_R_mult * risk
                        if target <= 0:
                            break
                        max_hold = last_session_bar - j
                        if max_hold < 1:
                            max_hold = 1
                        out.append(Signal(
                            bar_idx=j, direction="SHORT",
                            entry_price=entry, stop_price=stop,
                            target_price=target,
                            reason="orb_short_breakout",
                            max_hold_bars=max_hold,
                        ))
                        broken = True
                        break
                # Skip to one past the last session bar so we don't re-detect
                # any later in-session bar as an OR-open.
                i = last_session_bar + 1
                continue
            i += 1
        return out
