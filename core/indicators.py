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
import pandas as pd  # noqa: F401


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


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    """Rolling maximum over `period` bars, INCLUDING the current bar.

    For Donchian breakout we usually want the max EXCLUDING the current bar
    (else trivially equals close). Caller can shift(1) to do that.
    """
    if period < 1:
        raise ValueError(f"period must be >= 1; got {period}")
    return series.rolling(period, min_periods=period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    """Rolling minimum over `period` bars, INCLUDING the current bar."""
    if period < 1:
        raise ValueError(f"period must be >= 1; got {period}")
    return series.rolling(period, min_periods=period).min()


def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index using Wilder's smoothing (matches MT5).

    Mathematical definition:
        delta = price[i] - price[i-1]
        gain  = max(delta, 0)    loss = max(-delta, 0)
        avg_gain[i] = (1/period) × gain[i] + (1 - 1/period) × avg_gain[i-1]
        avg_loss[i] = (1/period) × loss[i] + (1 - 1/period) × avg_loss[i-1]
        RS  = avg_gain / avg_loss
        RSI = 100 - 100 / (1 + RS)

    Boundary handling:
      - First bar:               NaN (no prior price)
      - avg_gain == avg_loss == 0:  50  (no movement = neutral)
      - avg_loss == 0, avg_gain > 0: 100 (only up moves)
      - avg_gain == 0, avg_loss > 0: 0   (only down moves)
    """
    if period < 1:
        raise ValueError(f"period must be >= 1; got {period}")
    delta = series.diff()
    # Replace the first NaN with 0 so EWM starts from a defined state,
    # but we'll keep the NaN in the OUTPUT for that bar.
    first_nan_mask = delta.isna()
    gain = delta.clip(lower=0).fillna(0)
    loss = (-delta).clip(lower=0).fillna(0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    rsi = pd.Series(np.nan, index=series.index, dtype=float)
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    only_gain = (avg_loss == 0) & (avg_gain > 0)
    only_loss = (avg_gain == 0) & (avg_loss > 0)
    normal = ~(both_zero | only_gain | only_loss)

    rsi[both_zero] = 50.0
    rsi[only_gain] = 100.0
    rsi[only_loss] = 0.0
    rs = avg_gain[normal] / avg_loss[normal]
    rsi[normal] = 100.0 - 100.0 / (1.0 + rs)
    # Bar 0 stays NaN (no prior price defined)
    rsi[first_nan_mask] = np.nan
    return rsi


def bollinger_bands(series: pd.Series, period: int = 20,
                     k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands: (upper, mid, lower).

    mid   = SMA(close, period)
    upper = mid + k × stdev(close, period)
    lower = mid - k × stdev(close, period)

    stdev uses sample standard deviation (ddof=0) to match MT5's iBands().
    """
    if period < 1:
        raise ValueError(f"period must be >= 1; got {period}")
    mid = series.rolling(period, min_periods=period).mean()
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower
