"""
test_cointegration.py — verify the pure-math primitives.

We test on synthetic series where the answer is known:
1. Two cointegrated series → ADF rejects null
2. Two random walks → ADF fails to reject
3. Hedge ratio recovery on a constructed pair
4. Z-score basics
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core import cointegration as ci


def test_zscore_centred():
    s = pd.Series(np.arange(100, dtype=float))
    z = ci.z_score(s, window=20)
    # z should oscillate around 0 for a linear series
    # Last value should be positive (above rolling mean)
    assert z.iloc[-1] > 0


def test_zscore_constant_series_returns_nan():
    s = pd.Series([5.0] * 50)
    z = ci.z_score(s, window=20)
    # stdev is 0 → z is NaN
    assert pd.isna(z.iloc[-1])


def test_hedge_ratio_recovers_known_beta():
    # Construct b = uniform random; a = 2.5 × b + small noise
    rng = np.random.default_rng(42)
    b = pd.Series(rng.uniform(50, 150, size=500))
    noise = pd.Series(rng.normal(0, 0.5, size=500))
    a = 2.5 * b + noise
    beta = ci.regress_hedge_ratio(a, b, lookback=200)
    # The last computed beta should be close to 2.5
    assert abs(beta.iloc[-1] - 2.5) < 0.1


def test_adf_rejects_stationary_series():
    # AR(1) with phi < 1 → stationary
    rng = np.random.default_rng(7)
    n = 500
    eps = rng.normal(0, 1, n)
    s = np.zeros(n)
    for i in range(1, n):
        s[i] = 0.5 * s[i - 1] + eps[i]
    p = ci.adf_pvalue(pd.Series(s))
    # p should be small (likely < 0.05)
    assert p < 0.10  # loose threshold to handle fallback path


def test_adf_does_NOT_reject_random_walk():
    # Pure random walk → non-stationary
    rng = np.random.default_rng(11)
    n = 500
    s = np.cumsum(rng.normal(0, 1, n))
    p = ci.adf_pvalue(pd.Series(s))
    # p should be high (> 0.10)
    assert p > 0.10


def test_cointegration_passes_on_constructed_pair():
    rng = np.random.default_rng(3)
    n = 500
    # b is a random walk
    b = pd.Series(np.cumsum(rng.normal(0, 1, n)))
    # a = 0.8 × b + AR(1) noise (stationary residual → cointegrated)
    eps = rng.normal(0, 1, n)
    noise = np.zeros(n)
    for i in range(1, n):
        noise[i] = 0.5 * noise[i - 1] + eps[i]
    a = 0.8 * b + pd.Series(noise)
    # Cointegrated series should pass at 10% confidence
    assert ci.cointegration_passes(a, b, lookback=200, alpha=0.10)


def test_cointegration_FAILS_on_independent_random_walks():
    rng = np.random.default_rng(99)
    n = 500
    a = pd.Series(np.cumsum(rng.normal(0, 1, n)))
    b = pd.Series(np.cumsum(rng.normal(0, 1, n)))
    # Two independent random walks should NOT cointegrate
    assert not ci.cointegration_passes(a, b, lookback=200, alpha=0.05)


def test_compute_spread_basic():
    a = pd.Series([10.0, 11.0, 12.0])
    b = pd.Series([5.0, 5.5, 6.0])
    beta = pd.Series([2.0, 2.0, 2.0])
    spread = ci.compute_spread(a, b, beta)
    # spread should be 0 when a = β×b exactly
    assert all(abs(s) < 1e-9 for s in spread)


def test_regress_hedge_ratio_validates_inputs():
    a = pd.Series([1.0, 2.0])
    b = pd.Series([1.0, 2.0, 3.0])
    try:
        ci.regress_hedge_ratio(a, b, lookback=20)
        assert False, "should have raised"
    except ValueError:
        pass

    a = pd.Series([1.0] * 20)
    b = pd.Series([1.0] * 20)
    try:
        ci.regress_hedge_ratio(a, b, lookback=5)
        assert False, "should have raised"
    except ValueError:
        pass
