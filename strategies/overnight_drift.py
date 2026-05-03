"""
overnight_drift.py — capture the overnight equity premium on indices.

The "overnight effect": on US equity indices (S&P 500, NAS100, etc.),
the bulk of the multi-decade upside drift has historically occurred
OVERNIGHT — i.e., between the close of one session and the open of the
next — rather than during the regular session. The intraday return
component has a much weaker / often negative drift.

Adaptation to D1 candles:
  buy at close of every D1 bar, exit at close of NEXT D1 bar.
  Holding period is exactly 1 D1 bar = ~1 calendar day.
  This isn't a perfect "close-to-open" capture (since D1 close → next D1 close
  spans both overnight AND the next session), but it's the cleanest realization
  available with D1 data and still tilts toward overnight return for indices
  where overnight is the larger component of total return.

Stop / target are set wide so they rarely fire — exits are time-based
(max_hold_bars=1). This is by design: overnight_drift is a holding-period
strategy, not a stop/target strategy.

LONG-ONLY by definition (the documented effect is one-directional uplift).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import atr_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class OvernightDriftParams:
    atr_period: int = 14
    stop_atr_mult: float = 6.0      # very wide so SL rarely fires; time exit dominates
    target_atr_mult: float = 6.0    # very wide TP for the same reason
    max_hold: int = 1               # close at NEXT bar's close
    long_only: bool = True          # by design


class OvernightDrift:
    name = "overnight_drift"

    def __init__(self, params: OvernightDriftParams = OvernightDriftParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < p.atr_period + 2:
            return []

        c = candles["close"].to_numpy()
        atr = atr_wilder(candles, p.atr_period).to_numpy()

        out: list[Signal] = []
        # Skip the very last bar — there's no "next bar" to close into; the
        # backtester's EOD logic would still close it, but the trade duration
        # would be 0 bars which violates the strategy's 1-bar-hold semantic.
        for i in range(p.atr_period, n - 1):
            a = atr[i]
            if a <= 0:
                continue
            entry = float(c[i])
            stop = entry - p.stop_atr_mult * a
            target = entry + p.target_atr_mult * a
            if stop <= 0:
                continue
            out.append(Signal(
                bar_idx=i, direction="LONG",
                entry_price=entry, stop_price=float(stop),
                target_price=float(target),
                reason="overnight_drift",
                max_hold_bars=p.max_hold,
            ))
        return out
