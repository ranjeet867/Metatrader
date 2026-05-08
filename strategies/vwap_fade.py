"""
vwap_fade.py — session-anchored VWAP mean-reversion.

Mechanism (Two Sigma intraday classic):
When intraday price diverges materially from session VWAP without a
fundamental driver, institutional flow tends to fade the move back
toward VWAP. Works on liquid instruments during the middle of a
session — too early = open noise, too late = positioning.

Implementation:
1. For each UTC day, anchor a session VWAP at the FIRST bar of the day.
2. Compute rolling stdev of (price - VWAP) over the same session.
3. Entry:
     LONG  when close < VWAP - k_entry × σ  (oversold from VWAP)
     SHORT when close > VWAP + k_entry × σ  (overbought from VWAP)
4. Stop = VWAP ± k_stop × σ (further from mean = larger stop). Tight.
5. Target = back to VWAP (R:R ≈ 0.7-0.9, but high WR balances it).

Session window: skip first `skip_open_bars` and last `skip_close_bars`
to avoid open-noise + close-positioning. This is THE filter that
separates a real edge from a curve-fitted one.

Defaults are NOT tuned per-cell — same params on every ticker × TF.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.strategy import Signal


@dataclass(frozen=True)
class VwapFadeParams:
    k_entry: float = 1.5            # σ from VWAP for entry
    k_stop: float = 2.5             # σ from VWAP for stop
    skip_open_bars: int = 4         # avoid open noise (first hour on M15)
    skip_close_bars: int = 4        # avoid close positioning (last hour)
    min_session_bars: int = 16      # skip thin/holiday days
    long_only: bool = False


class VwapFade:
    name = "vwap_fade"

    def __init__(self, params: VwapFadeParams = VwapFadeParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        if len(candles) < p.min_session_bars + 4:
            return []

        # Per-day session grouping on UTC date
        if "time" in candles.columns:
            ts = pd.to_datetime(candles["time"], utc=True)
        else:
            ts = pd.to_datetime(candles.index, utc=True)
        utc_date = ts.dt.date.values

        c = candles["close"].values
        h = candles["high"].values
        lo_ = candles["low"].values
        # Typical price weighted by volume if present, else close
        if "volume" in candles.columns:
            v = candles["volume"].fillna(1.0).values
        else:
            v = np.ones(len(candles))
        # Use HLC/3 as VWAP-typical price input (close-only over-weights last tick)
        tp = (h + lo_ + c) / 3.0

        out: list[Signal] = []
        n = len(candles)
        i = 0
        while i < n:
            day = utc_date[i]
            day_start = i
            while i < n and utc_date[i] == day:
                i += 1
            day_end = i  # exclusive
            day_len = day_end - day_start
            if day_len < p.min_session_bars:
                continue

            # Build session-anchored cumulative VWAP and price deviation σ
            vol_d = v[day_start:day_end]
            tp_d = tp[day_start:day_end]
            cum_vol = np.cumsum(vol_d)
            cum_pv = np.cumsum(tp_d * vol_d)
            # Avoid div-by-zero on volume-less first bar
            with np.errstate(divide="ignore", invalid="ignore"):
                vwap_d = np.where(cum_vol > 0, cum_pv / cum_vol, tp_d)

            # Rolling σ of (close - VWAP) over expanding session window
            dev = c[day_start:day_end] - vwap_d
            # Use simple expanding stdev — it stabilises after a few bars
            sigma_d = pd.Series(dev).expanding(min_periods=2).std().values

            # Walk the trading window of the day
            scan_lo = day_start + p.skip_open_bars
            scan_hi = day_end - p.skip_close_bars
            for j_global in range(scan_lo, scan_hi):
                k = j_global - day_start  # local idx
                if k < 0 or k >= len(vwap_d):
                    continue
                sig = sigma_d[k]
                vw = vwap_d[k]
                if sig is None or pd.isna(sig) or sig <= 0 or pd.isna(vw):
                    continue
                cls = c[j_global]
                # LONG: oversold below VWAP
                if cls < vw - p.k_entry * sig:
                    stop = vw - p.k_stop * sig
                    target = vw  # revert to mean
                    if stop > 0 and stop < cls and target > cls:
                        out.append(Signal(
                            bar_idx=j_global, direction="LONG",
                            entry_price=float(cls),
                            stop_price=float(stop),
                            target_price=float(target),
                            reason=f"vwap_fade_long_dev={(cls-vw)/sig:.2f}σ",
                        ))
                    continue
                if p.long_only:
                    continue
                # SHORT: overbought above VWAP
                if cls > vw + p.k_entry * sig:
                    stop = vw + p.k_stop * sig
                    target = vw
                    if stop > cls and target < cls and target > 0:
                        out.append(Signal(
                            bar_idx=j_global, direction="SHORT",
                            entry_price=float(cls),
                            stop_price=float(stop),
                            target_price=float(target),
                            reason=f"vwap_fade_short_dev={(cls-vw)/sig:.2f}σ",
                        ))
        return out
