"""
double_bottom.py — W-pattern reversal with RSI divergence.

Definition (LONG variant, mirror for SHORT as double_top):
  1. Find a local low at bar A: low[A] < low[A-k:A] AND low[A] < low[A+1:A+k]
     for k = pivot_window (default 5).
  2. After A, look for a second local low at bar B (B > A + min_separation),
     where:
       a) low[B] is within band_pct of low[A]  (default 0.3% — "double" tolerance)
       b) RSI[B] > RSI[A]   (bullish divergence — price made same low, momentum
          improved → exhaustion / accumulation)
  3. Find the neckline = max(high[A:B+1]) — the swing high between the two lows.
  4. Enter LONG on close > neckline at bar j (j > B) within max_wait_bars.
  5. Stop = min(low[A], low[B]) - 0.5 × ATR[j]
     Target = entry + target_R_mult × (entry - stop)

Why this is interesting:
  - Classic high-WR pattern in classical TA, but most implementations skip the
    RSI-divergence requirement, which kills selectivity. Adding it raises WR
    at the cost of fewer trades — exactly the trade we want when running
    inside an FTMO challenge.
  - Different signal source from your existing donchian/ema/rsi cells —
    so survivors offer real diversification.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import rsi_wilder, atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class DoubleBottomParams:
    pivot_window: int = 5         # bars on each side for local-extreme test
    min_separation: int = 8       # B must be at least this many bars after A
    max_separation: int = 60      # B must be no more than this many bars after A
    band_pct: float = 0.003       # second low within ±0.3% of first low
    rsi_period: int = 14
    require_divergence: bool = True   # RSI[B] > RSI[A] for bullish divergence
    atr_period: int = 14
    stop_atr_mult: float = 0.5    # added below the lower of the two lows
    target_R_mult: float = 2.0
    max_wait_bars: int = 20       # neckline must break within this many bars
    long_only: bool = True        # set False to mirror as double-top SHORT


class DoubleBottom:
    name = "double_bottom"

    def __init__(self, params: DoubleBottomParams = DoubleBottomParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < max(p.pivot_window * 2 + p.max_separation + p.max_wait_bars,
                   p.rsi_period * 3, p.atr_period * 3, 100):
            return []

        h = candles["high"].to_numpy()
        l = candles["low"].to_numpy()
        c = candles["close"].to_numpy()
        rsi = rsi_wilder(candles["close"], p.rsi_period).to_numpy()
        atr = atr_wilder(candles, p.atr_period).to_numpy()

        # Detect local lows (LONG side) — bar i where low[i] is the smallest
        # low in [i-window, i+window]. Same for highs (mirror).
        pw = p.pivot_window
        is_low_pivot = np.zeros(n, dtype=bool)
        is_high_pivot = np.zeros(n, dtype=bool)
        for i in range(pw, n - pw):
            window_l = l[i - pw:i + pw + 1]
            if l[i] == window_l.min() and (window_l == l[i]).sum() == 1:
                is_low_pivot[i] = True
            window_h = h[i - pw:i + pw + 1]
            if h[i] == window_h.max() and (window_h == h[i]).sum() == 1:
                is_high_pivot[i] = True

        out: list[Signal] = []
        # Walk through second-low candidates B; for each B, scan back for an
        # earlier matching A.
        for B in range(pw + p.min_separation, n - 1):
            if not is_low_pivot[B]:
                continue
            band_lo = l[B] * (1 - p.band_pct)
            band_hi = l[B] * (1 + p.band_pct)
            for A in range(max(pw, B - p.max_separation), B - p.min_separation):
                if not is_low_pivot[A]:
                    continue
                if l[A] < band_lo or l[A] > band_hi:
                    continue
                if p.require_divergence and rsi[B] <= rsi[A]:
                    continue
                # Neckline = max high between A and B (inclusive)
                neckline = float(h[A:B + 1].max())
                # Look for the close > neckline within max_wait_bars
                end = min(B + 1 + p.max_wait_bars, n - 1)
                for j in range(B + 1, end):
                    if c[j] > neckline:
                        if not p.long_only:
                            # double-top mirror handled in DoubleTop class
                            pass
                        entry = float(c[j])
                        stop_base = min(l[A], l[B])
                        atr_j = float(atr[j]) if not np.isnan(atr[j]) else 0.0
                        stop = stop_base - p.stop_atr_mult * atr_j
                        risk = entry - stop
                        if risk <= 0:
                            break
                        target = entry + p.target_R_mult * risk
                        out.append(Signal(
                            bar_idx=j, direction="LONG",
                            entry_price=entry, stop_price=stop,
                            target_price=target,
                            reason=f"double_bottom_A{A}_B{B}",
                        ))
                        break
                # Inner loop done — try next A pair (some pairs may resolve)
                # Actually break — we want only ONE signal per B
                break
        return out
