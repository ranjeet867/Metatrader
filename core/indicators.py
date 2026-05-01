"""
indicators.py — pure functions for technical indicators.

Every function here is:
  - Pure (no side effects, no I/O)
  - Deterministic (same input → same output)
  - Tested against analytical formulas in tests/test_indicators.py

We use formulas that match MT5's defaults exactly:
  - EMA: standard exponential, alpha = 2 / (period + 1)
  - ATR: Wilder's (RMA), alpha = 1 / period
  - True Range: max(H-L, |H-C_prev|, |L-C_prev|)

If a function's output differs from MT5 by more than 1e-4 on real data,
we have a bug — fix it here, not by changing tests.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average.

    Mathematical definition:
        alpha = 2 / (period + 1)
        EMA[0] = series[0]
        EMA[i] = alpha × series[i] + (1 - alpha) × EMA[i-1]

    Matches pandas `ewm(span=period, adjust=False)` and MT5 `iMA(MODE_EMA)`.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1; got {period}")
    return series.ewm(span=period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range per bar.

        TR[i] = max(H[i] - L[i], |H[i] - C[i-1]|, |L[i] - C[i-1]|)

    For the FIRST bar (no prior close), TR = H - L.
    """
    h = df["high"]
    l = df["low"]
    c_prev = df["close"].shift(1)
    tr = pd.concat([
        h - l,
        (h - c_prev).abs(),
        (l - c_prev).abs(),
    ], axis=1).max(axis=1)
    # First bar: c_prev is NaN; the H-L term is the only valid one
    tr.iloc[0] = h.iloc[0] - l.iloc[0]
    return tr


def atr_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing (a.k.a. RMA).

    Wilder's EMA has alpha = 1 / period (different from standard EMA).

    Matches MT5's iATR() exactly.

    Mathematical definition:
        alpha = 1 / period
        ATR[i] = alpha × TR[i] + (1 - alpha) × ATR[i-1]

    Equivalent to pandas `ewm(alpha=1/period, adjust=False)`.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1; got {period}")
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()
