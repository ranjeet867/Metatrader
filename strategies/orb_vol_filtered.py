"""
orb_vol_filtered.py — Open-Range Breakout with realised-vol expansion filter.

Mechanism (institutional intraday):
Index futures and FX majors release information packets at session open.
The first N bars (typically 30-60min) form an information range; breaks
of that range CONTINUE when realised volatility is expanding, FADE when
it's compressing. Most retail ORB strategies skip the regime filter and
get chopped to bits in low-vol days.

Pattern: For each calendar UTC day, compute the high/low of the FIRST
`opening_bars` bars (e.g. first 4 × M15 = 60min). Once that range is
locked in, take a break ABOVE the range high (long) or BELOW range low
(short) ONLY IF realised-vol short-window > vol_mult × long-window.

One signal per day per direction (no re-entry on the same break-leg).
After `cutoff_bars_into_day` we stop generating signals to avoid late-
day false breakouts where range becomes meaningless.

Defaults (anti-curve-fit):
- opening_bars=4 → 60min on M15, 4h on H1; round number, no per-cell tune
- vol_mult=1.2 → looser than vol_break since ORB is less noisy intrinsically
- cutoff_bars_into_day=64 (16h on M15, 16 bars on H1) → covers FX day
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class OrbVolFilteredParams:
    opening_bars: int = 4
    short_vol_window: int = 20
    long_vol_window: int = 100
    vol_mult: float = 1.2
    atr_period: int = 14
    stop_atr_mult: float = 1.0      # tight stop — break failure shows fast
    target_atr_mult: float = 2.0    # 2:1 R:R
    cutoff_bars_into_day: int = 64
    long_only: bool = False


class OrbVolFiltered:
    name = "orb_vol_filtered"

    def __init__(self,
                   params: OrbVolFilteredParams = OrbVolFilteredParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        warmup = max(p.opening_bars, p.atr_period, p.long_vol_window) + 2
        if len(candles) < warmup:
            return []

        atr = atr_wilder(candles, p.atr_period)
        log_ret = np.log(candles["close"] / candles["close"].shift(1))
        rv_short = log_ret.rolling(p.short_vol_window).std()
        rv_long = log_ret.rolling(p.long_vol_window).std()
        vol_ratio = (rv_short / rv_long).values

        # Build per-day groupings on UTC date
        if "time" in candles.columns:
            ts = pd.to_datetime(candles["time"], utc=True)
        else:
            # fall back to index if `time` column not present
            ts = pd.to_datetime(candles.index, utc=True)
        utc_date = ts.dt.date.values

        out: list[Signal] = []
        c = candles["close"].values
        h = candles["high"].values
        lo_ = candles["low"].values
        atr_v = atr.values
        n = len(candles)

        # Walk day-by-day. For each day: lock range from first opening_bars,
        # then look for breakouts during the trading window.
        i = 0
        while i < n:
            # Find the start of this day in the array
            day = utc_date[i]
            day_start = i
            while i < n and utc_date[i] == day:
                i += 1
            day_end = i  # exclusive

            day_len = day_end - day_start
            if day_len < p.opening_bars + 2:
                continue  # day too short (weekend roll, holiday)

            # Range = high/low of first opening_bars bars of the day
            ob_end = day_start + p.opening_bars
            range_hi = float(h[day_start:ob_end].max())
            range_lo = float(lo_[day_start:ob_end].min())

            # Walk the rest of the day looking for a break
            broke_long = broke_short = False
            scan_end = min(day_end, day_start + p.cutoff_bars_into_day)
            for j in range(ob_end, scan_end):
                a = atr_v[j]
                if a <= 0:
                    continue
                vr = vol_ratio[j]
                if pd.isna(vr) or vr < p.vol_mult:
                    continue
                # LONG break — close above range high
                if not broke_long and c[j] > range_hi:
                    stop = c[j] - p.stop_atr_mult * a
                    target = c[j] + p.target_atr_mult * a
                    if stop > 0:
                        out.append(Signal(
                            bar_idx=j, direction="LONG",
                            entry_price=float(c[j]), stop_price=float(stop),
                            target_price=float(target),
                            reason=f"orb_long_vr={vr:.2f}",
                        ))
                    broke_long = True
                    continue
                # SHORT break — close below range low
                if (not p.long_only) and (not broke_short) and c[j] < range_lo:
                    stop = c[j] + p.stop_atr_mult * a
                    target = c[j] - p.target_atr_mult * a
                    if target > 0:
                        out.append(Signal(
                            bar_idx=j, direction="SHORT",
                            entry_price=float(c[j]), stop_price=float(stop),
                            target_price=float(target),
                            reason=f"orb_short_vr={vr:.2f}",
                        ))
                    broke_short = True
        return out
