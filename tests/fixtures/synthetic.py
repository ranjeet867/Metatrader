"""
synthetic.py — candle generators with mathematically-derivable properties.

Every generator is DETERMINISTIC (no randomness) so tests can assert
exact expected outputs. Each one is paired with a closed-form formula
the test can use to verify our indicator/strategy/backtester math.

Convention: all candles use seconds-since-epoch UTC for time, sorted ascending.
Columns: time (datetime64[ns, UTC]), open, high, low, close, volume.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 1. CONSTANT — every bar has the same OHLC. Tests "no movement" cases.
#
#   Expected:
#     EMA(N) at every bar      = price
#     ATR(N) at every bar      = 0 (no movement = no range)
#     Any cross-based signal   = never fires
#     Any volatility filter    = stays in vol_too_low regime
# ---------------------------------------------------------------------------
def constant(price: float = 100.0, n_bars: int = 100,
              start: str = "2024-01-01", freq: str = "h") -> pd.DataFrame:
    """N bars with OHLC all equal to `price`."""
    times = pd.date_range(start, periods=n_bars, freq=freq, tz="UTC")
    return pd.DataFrame({
        "time": times,
        "open": np.full(n_bars, price, dtype=float),
        "high": np.full(n_bars, price, dtype=float),
        "low":  np.full(n_bars, price, dtype=float),
        "close": np.full(n_bars, price, dtype=float),
        "volume": np.full(n_bars, 1000, dtype=float),
    })


# ---------------------------------------------------------------------------
# 2. LINEAR RAMP — close[i] = start + i × step.
#
#   Expected:
#     ATR(N) when range = 0     = 0 (we set high = low = close here too)
#     EMA(N) lags the close by ~N/2 bars in steady state
#     With high=low=close, no SL/TP can ever fire intra-bar
# ---------------------------------------------------------------------------
def linear_ramp(start_price: float = 100.0, step: float = 0.1,
                 n_bars: int = 100, start: str = "2024-01-01",
                 freq: str = "h") -> pd.DataFrame:
    """close[i] = start_price + i × step, with high = low = close."""
    times = pd.date_range(start, periods=n_bars, freq=freq, tz="UTC")
    closes = start_price + np.arange(n_bars, dtype=float) * step
    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": closes,
        "low":  closes,
        "close": closes,
        "volume": np.full(n_bars, 1000, dtype=float),
    })


# ---------------------------------------------------------------------------
# 3. STEP FUNCTION — N bars at low_price, then N bars at high_price.
#
#   Expected:
#     EMA(period) eventually transitions from low_price to high_price.
#     EMA(N) at bar (transition + k) follows analytical formula:
#         E[k] = high + (low - high) × (1 - alpha)^(k+1)
#       where alpha = 2 / (period + 1)
#     Useful for testing crossover signals — guaranteed to fire once.
# ---------------------------------------------------------------------------
def step_function(low_price: float = 100.0, high_price: float = 110.0,
                   bars_at_low: int = 100, bars_at_high: int = 100,
                   start: str = "2024-01-01", freq: str = "h") -> pd.DataFrame:
    """First half at low, second half at high. high = low = close per bar."""
    n = bars_at_low + bars_at_high
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")
    closes = np.concatenate([
        np.full(bars_at_low, low_price),
        np.full(bars_at_high, high_price),
    ])
    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": closes,
        "low":  closes,
        "close": closes,
        "volume": np.full(n, 1000, dtype=float),
    })


# ---------------------------------------------------------------------------
# 4. SAWTOOTH — repeating up-down pattern with KNOWN crossovers.
#
#   Useful for guaranteeing N signal events at predictable bar indices.
#   Pattern: rises for `up_bars`, falls for `down_bars`, repeats.
#
#   For EMA9/EMA20 cross detection: with up_bars=20, down_bars=20,
#   crosses occur ~at the inflection points of each cycle.
# ---------------------------------------------------------------------------
def sawtooth(low_price: float = 100.0, high_price: float = 110.0,
              up_bars: int = 20, down_bars: int = 20, n_cycles: int = 5,
              start: str = "2024-01-01", freq: str = "h") -> pd.DataFrame:
    """Up-down repeating pattern. high = low = close per bar."""
    cycle_len = up_bars + down_bars
    n = cycle_len * n_cycles
    times = pd.date_range(start, periods=n, freq=freq, tz="UTC")

    closes = np.empty(n, dtype=float)
    for c in range(n_cycles):
        offset = c * cycle_len
        # rising leg
        up_step = (high_price - low_price) / up_bars
        for i in range(up_bars):
            closes[offset + i] = low_price + i * up_step
        # falling leg
        down_step = (high_price - low_price) / down_bars
        for i in range(down_bars):
            closes[offset + up_bars + i] = high_price - i * down_step

    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": closes,
        "low":  closes,
        "close": closes,
        "volume": np.full(n, 1000, dtype=float),
    })


# ---------------------------------------------------------------------------
# 5. TRADE TRACK — tiny series engineered so a specific strategy makes
#    exactly the trades we specify, hitting exact SL or TP prices.
#
#   Used by backtest tests where we KNOW the exact PnL outcome.
# ---------------------------------------------------------------------------
def hand_crafted_trade(entry_bar: int, entry_price: float,
                        stop_price: float, target_price: float,
                        outcome: str, n_bars: int = 50,
                        start: str = "2024-01-01", freq: str = "h") -> pd.DataFrame:
    """
    Build a candle series where a long trade entered at `entry_bar` will
    deterministically hit either SL or TP, with no ambiguity (no bar
    contains both stop and target levels).

    outcome ∈ {"hit_target", "hit_stop"}.

    The series:
      - bars 0..entry_bar-1: flat at entry_price (no signal yet)
      - bar entry_bar:       open=entry_price  (trade opens here)
      - bars entry_bar+1..end:
          if hit_target: trickle UP, hit target at last bar
          if hit_stop:   trickle DOWN, hit stop at last bar
    """
    if outcome not in ("hit_target", "hit_stop"):
        raise ValueError(f"unknown outcome: {outcome}")
    if entry_bar < 1 or entry_bar >= n_bars:
        raise ValueError(f"entry_bar must be in [1, {n_bars}); got {entry_bar}")

    times = pd.date_range(start, periods=n_bars, freq=freq, tz="UTC")
    closes = np.full(n_bars, entry_price, dtype=float)

    # After entry_bar, ramp toward the target outcome
    if outcome == "hit_target":
        end_price = target_price
    else:
        end_price = stop_price
    bars_to_ramp = n_bars - entry_bar - 1
    if bars_to_ramp > 0:
        ramp = np.linspace(entry_price, end_price, bars_to_ramp + 1)
        closes[entry_bar:] = ramp[: n_bars - entry_bar]

    # high = low = close everywhere except the FINAL bar which actually
    # touches the target/stop level intra-bar so the executor can fill.
    highs = closes.copy()
    lows = closes.copy()
    if outcome == "hit_target":
        highs[-1] = target_price
        lows[-1] = closes[-1]
    else:
        lows[-1] = stop_price
        highs[-1] = closes[-1]

    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": highs,
        "low":  lows,
        "close": closes,
        "volume": np.full(n_bars, 1000, dtype=float),
    })
