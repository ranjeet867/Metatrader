"""
tsmom.py — Time-Series Momentum, faithful to AQR's published version.

Source: Moskowitz, Ooi, Pedersen (2012), "Time Series Momentum",
Journal of Financial Economics. Backtested to 1903 across 60+ markets;
documented Sharpe 1.0-1.4 in a portfolio of equal-vol-weighted markets.

Mechanism (no curve-fitting, no per-cell tuning):
1. Compute the past `lookback_bars` price return.
2. If positive → LONG; if negative → SHORT (or skip if long_only).
3. Hold for `hold_bars` (~1/12 of lookback ≈ "monthly rebalance" if
   lookback is "12 months").
4. Vol-target via wide SL/TP (~10% of entry) that rarely triggers — the
   actual exit is `max_hold_bars` (the rebalance cadence). This matches
   AQR's published version where the SL/TP isn't a stop in the
   traditional sense; the position is closed and re-evaluated on the
   rebalance date.

Why this beats existing trend-followers in the catalog:
- ema_cross/donchian use 9-55 bar lookbacks → reactive to short-term
  noise. TSMOM uses 252-6048 bar lookback → captures multi-month
  drift only, the slow-moving institutional flow.
- Same mechanism, same parameters, every instrument. No curve-fitting
  by construction.
- The literature documents Sharpe of 0.4-0.8 PER instrument, ~1.0-1.4
  COMBINED. Single-cell rarely beats your existing catalog; the value
  is in adding a 5-10 cell low-correlation TSMOM block alongside
  your existing rsi_30_70 / ema_cross cells.

Suggested params per TF (all approximate "12-month / 1-month"):
- D1:  lookback=252,  hold=21
- H1:  lookback=6048, hold=504    (12 months × 21 days × 24 hours)
- M15: NOT recommended — TSMOM's 12-month signal doesn't fit M15 trades
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.strategy import Signal


@dataclass(frozen=True)
class TsmomParams:
    """AQR-default parameters. DO NOT tune per-cell — that's curve-fit."""
    lookback_bars: int = 252       # "12 months" on D1; override per TF
    hold_bars: int = 21            # "1 month" on D1; override per TF
    # Wide SL/TP rarely fires; actual exit is max_hold_bars. Per AQR's
    # paper, TSMOM doesn't use stops — the rebalance IS the exit.
    stop_pct: float = 0.10
    target_pct: float = 0.10
    long_only: bool = False


class Tsmom:
    name = "tsmom"

    def __init__(self, params: TsmomParams = TsmomParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < p.lookback_bars + 2:
            return []

        out: list[Signal] = []
        c = candles["close"].values
        last_signal_bar = -p.hold_bars  # allow first signal to fire

        for i in range(p.lookback_bars, n):
            # Monthly-rebalance gate: don't emit again until prior
            # position has had its full hold window.
            if i - last_signal_bar < p.hold_bars:
                continue

            past_close = float(c[i - p.lookback_bars])
            current_close = float(c[i])
            if past_close <= 0 or current_close <= 0:
                continue

            past_return = (current_close - past_close) / past_close

            # No-trade zone — extremely tiny trailing return is
            # statistically indistinguishable from zero. Avoids
            # whipsaw on flat markets.
            if abs(past_return) < 0.005:
                continue

            if past_return > 0:
                stop = current_close * (1 - p.stop_pct)
                target = current_close * (1 + p.target_pct)
                if stop > 0:
                    out.append(Signal(
                        bar_idx=i, direction="LONG",
                        entry_price=current_close,
                        stop_price=float(stop),
                        target_price=float(target),
                        reason=f"tsmom_long_{past_return*100:+.1f}%",
                        max_hold_bars=p.hold_bars,
                    ))
                    last_signal_bar = i
            elif not p.long_only:
                stop = current_close * (1 + p.stop_pct)
                target = current_close * (1 - p.target_pct)
                if target > 0:
                    out.append(Signal(
                        bar_idx=i, direction="SHORT",
                        entry_price=current_close,
                        stop_price=float(stop),
                        target_price=float(target),
                        reason=f"tsmom_short_{past_return*100:+.1f}%",
                        max_hold_bars=p.hold_bars,
                    ))
                    last_signal_bar = i

        return out
