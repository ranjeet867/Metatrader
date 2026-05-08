"""
cointegration.py — pure-math primitives for pairs trading.

Functions
---------
- regress_hedge_ratio(a, b, lookback): rolling OLS slope; returns β series
- compute_spread(a, b, beta): spread_t = a_t - β_t × b_t
- adf_pvalue(series): Augmented Dickey-Fuller p-value (statsmodels)
- z_score(series, window): rolling z-score = (x − mean) / stdev

All functions take pandas Series and return pandas Series — no I/O,
no global state, easy to unit-test on synthetic data.

Why not import full statsmodels by default
------------------------------------------
statsmodels is a heavy import (1-2s). For the ADF test we use the
lightweight implementation that handles the common case (test for
stationarity at lag 1). When statsmodels IS available, we use its
adfuller() for proper critical values.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def regress_hedge_ratio(a: pd.Series, b: pd.Series,
                          lookback: int) -> pd.Series:
    """Rolling OLS regression: a = β × b + ε.

    Returns the β series (one value per bar after warmup). The hedge
    ratio answers "for every 1 unit of A, how many units of B do I need
    to short to be market-neutral?"

    Implementation: uses pd.rolling(lookback).cov / var which is the
    closed-form OLS slope without intercept centring (we centre on the
    rolling mean inside compute_spread).
    """
    if len(a) != len(b):
        raise ValueError(f"len mismatch: a={len(a)}, b={len(b)}")
    if lookback < 10:
        raise ValueError(f"lookback {lookback} < 10 — too noisy")

    # Rolling cov / rolling var = OLS slope
    a_mean = a.rolling(lookback).mean()
    b_mean = b.rolling(lookback).mean()
    cov_ab = ((a - a_mean) * (b - b_mean)).rolling(lookback).mean()
    var_b = ((b - b_mean) ** 2).rolling(lookback).mean()
    beta = cov_ab / var_b
    return beta


def compute_spread(a: pd.Series, b: pd.Series,
                     beta: pd.Series) -> pd.Series:
    """spread_t = a_t − β_t × b_t.

    The spread is what we trade. When stationary (passes ADF test)
    and we observe a z>2 deviation, we expect mean reversion.
    """
    if not (len(a) == len(b) == len(beta)):
        raise ValueError("all three series must have equal length")
    return a - beta * b


def z_score(series: pd.Series, window: int) -> pd.Series:
    """Rolling z-score of a series.

    z_t = (series_t − rolling_mean) / rolling_stdev

    Returns NaN for the first `window-1` bars (warmup) and at any bar
    where rolling_stdev is 0 (degenerate).
    """
    if window < 5:
        raise ValueError(f"window {window} < 5 — too noisy")
    rolling_mean = series.rolling(window).mean()
    rolling_std = series.rolling(window).std()
    # Replace zero stdev with NaN to avoid div-by-zero giving inf z
    rolling_std = rolling_std.where(rolling_std > 1e-12, np.nan)
    return (series - rolling_mean) / rolling_std


def adf_pvalue(series: pd.Series, max_lag: int = 1) -> float:
    """Augmented Dickey-Fuller test for stationarity.

    Returns the p-value. Lower p = stronger evidence of stationarity.
    Conventional threshold: p < 0.05 → stationary at 95% confidence.

    Uses statsmodels when available; falls back to a simple OLS-based
    approximation that's roughly correct but lacks proper critical
    values. Production paths should require statsmodels.
    """
    s = series.dropna()
    if len(s) < 30:
        return 1.0  # too few observations — fail-closed (not stationary)

    try:
        from statsmodels.tsa.stattools import adfuller
        result = adfuller(s.values, maxlag=max_lag, autolag=None,
                            regression="c")
        # adfuller returns (test_stat, p_value, used_lag, n_obs,
        #                    critical_values, ic_best)
        return float(result[1])
    except ImportError:
        # Lightweight fallback: regression of Δs on s_{t-1}
        # Test stat = coef / stderr; compare to MacKinnon table
        # approximation. This is APPROXIMATE — production should pip
        # install statsmodels for accurate critical values.
        diff = s.diff().dropna()
        lag1 = s.shift(1).dropna().reindex(diff.index)
        if len(diff) < 30 or lag1.std() == 0:
            return 1.0
        # Simple OLS: diff = α + β × lag1
        x = lag1.values
        y = diff.values
        n = len(x)
        x_mean = x.mean()
        y_mean = y.mean()
        ss_xx = ((x - x_mean) ** 2).sum()
        if ss_xx == 0:
            return 1.0
        beta = ((x - x_mean) * (y - y_mean)).sum() / ss_xx
        # Approximate p-value using t-stat with normal CDF
        residuals = y - (y_mean + beta * (x - x_mean))
        sigma = (residuals ** 2).sum() / (n - 2)
        if sigma <= 0:
            return 1.0
        se_beta = (sigma / ss_xx) ** 0.5
        t_stat = beta / se_beta if se_beta > 0 else 0.0
        # Rough mapping: t < -2.86 → p < 0.05 (MacKinnon 1996, n=∞)
        if t_stat < -3.43:
            return 0.01
        if t_stat < -2.86:
            return 0.05
        if t_stat < -2.57:
            return 0.10
        return 0.50


def cointegration_passes(a: pd.Series, b: pd.Series, *,
                            lookback: int = 200,
                            alpha: float = 0.05) -> bool:
    """One-shot test: are the two series cointegrated at confidence
    level (1-alpha)?

    1. Compute hedge ratio over `lookback` bars
    2. Compute the spread
    3. Run ADF on the spread
    4. Return True iff ADF p-value < alpha
    """
    if len(a) < lookback + 10:
        return False
    beta = regress_hedge_ratio(a, b, lookback)
    spread = compute_spread(a, b, beta)
    return adf_pvalue(spread.tail(lookback)) < alpha
