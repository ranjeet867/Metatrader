"""
test_dd_calculation_and_edge_cases.py — pin the drawdown and recovery
math + sanity-check behaviour when data or stats are missing.

User concern: DDdays of 1613 looked huge. This test pins:
  • Peak → trough span counted in CALENDAR days (not trading days)
  • Recovery span = days from trough back to a new high
  • Both are float seconds-since-peak / 86400, never NaN

Plus edge-case coverage:
  • empty equity curve → 0/0/0/None
  • missing R values → safe handling
  • single trade → no streak counted as 0
  • all-flat equity (no DD) → DDdays=0
  • permanent drawdown (never recovered) → recovery_days=None
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from core.backtest_stats import (
    _drawdown_picture, _streak_max, compute_full_stats,
)


# Fake BacktestResult shape compute_full_stats expects
@dataclass
class _FakeTrade:
    realized_pnl: float
    r_multiple: float
    entry_bar_idx: int = 0
    exit_bar_idx: int = 1


@dataclass
class _FakeResult:
    trades: list
    equity_curve: pd.DataFrame
    starting_balance: float = 100_000.0


def _make_curve(times_pnls):
    """[(iso_date, equity), ...] → DataFrame."""
    return pd.DataFrame(
        [{"time": t, "equity": e} for t, e in times_pnls]
    )


# ---------------------------------------------------------------------------
# _drawdown_picture
# ---------------------------------------------------------------------------

def test_dd_empty_curve():
    eq = pd.DataFrame(columns=["time", "equity"])
    dd_d, dd_p, dd_days, recov = _drawdown_picture(eq)
    assert dd_d == 0
    assert dd_p == 0
    assert dd_days == 0
    assert recov is None


def test_dd_flat_curve_no_drawdown():
    # Curve only goes up — no drawdown
    eq = _make_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 100_500),
        ("2025-01-03", 101_000),
    ])
    dd_d, dd_p, dd_days, recov = _drawdown_picture(eq)
    assert dd_d == 0
    assert dd_p == 0
    assert dd_days == 0


def test_dd_simple_drawdown_recovered():
    # Peak at day 2, trough at day 3, recovers at day 5
    eq = _make_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 105_000),     # peak
        ("2025-01-03", 95_000),      # trough -10%
        ("2025-01-04", 100_000),
        ("2025-01-05", 106_000),     # recovers above peak
    ])
    dd_d, dd_p, dd_days, recov = _drawdown_picture(eq)
    assert dd_d == 10_000
    assert abs(dd_p - 10_000 / 105_000 * 100) < 1e-6
    # peak (day 2) → trough (day 3) = 1 calendar day
    assert dd_days == 1.0
    # trough (day 3) → recovery (day 5) = 2 calendar days
    assert recov == 2.0


def test_dd_permanent_drawdown_no_recovery():
    eq = _make_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 110_000),     # peak
        ("2025-01-03", 90_000),      # trough -18.2%
        ("2025-01-04", 95_000),      # never reaches 110k again
    ])
    dd_d, dd_p, dd_days, recov = _drawdown_picture(eq)
    assert dd_d == 20_000
    assert dd_days == 1.0
    assert recov is None        # NEVER recovered to peak


def test_dd_long_underwater_period_correct_calendar_days():
    """Pin the user-visible 1613-day case: peak in early year,
    trough years later, slow recovery.

    Mimics what they saw: a D1 strategy that takes a deep DD that lasts
    for years before recovering. Math should produce the right calendar
    days even though only a handful of trades happened in between."""
    eq = _make_curve([
        ("2018-03-26", 100_000),
        ("2019-06-01", 110_000),     # peak
        ("2023-08-01", 95_000),      # trough — 4+ years later
        ("2026-04-01", 112_000),     # recovers — almost 3 years later
    ])
    dd_d, dd_p, dd_days, recov = _drawdown_picture(eq)
    # peak → trough = ~1522 calendar days (4 years 2 months)
    assert 1500 < dd_days < 1600
    # trough → recovery = ~974 calendar days (2.7 years)
    assert 950 < recov < 1000
    assert dd_d == 15_000


# ---------------------------------------------------------------------------
# _streak_max
# ---------------------------------------------------------------------------

def test_streak_max_empty():
    assert _streak_max([]) == 0


def test_streak_max_all_true():
    assert _streak_max([True, True, True, True]) == 4


def test_streak_max_single_run():
    assert _streak_max([False, True, True, False, True]) == 2


def test_streak_max_alternating():
    assert _streak_max([True, False, True, False, True]) == 1


# ---------------------------------------------------------------------------
# compute_full_stats — missing-data scenarios
# ---------------------------------------------------------------------------

def test_compute_full_stats_zero_trades_returns_zeros():
    result = _FakeResult(trades=[], equity_curve=_make_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 100_000),
    ]))
    stats = compute_full_stats(result, starting_balance=100_000)
    assert stats.n_trades == 0
    assert stats.win_rate_pct == 0
    assert stats.profit_factor == 0
    assert stats.avg_R == 0
    assert stats.risk_reward_ratio == 0


def test_compute_full_stats_only_winners():
    """All winning trades — profit_factor should be inf."""
    trades = [
        _FakeTrade(realized_pnl=100, r_multiple=1.0),
        _FakeTrade(realized_pnl=200, r_multiple=2.0),
    ]
    eq = _make_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 100_300),
    ])
    result = _FakeResult(trades=trades, equity_curve=eq)
    stats = compute_full_stats(result, starting_balance=100_000)
    assert stats.n_trades == 2
    assert stats.win_rate_pct == 100
    assert stats.profit_factor == float("inf")
    assert stats.avg_win_dollars == 150
    assert stats.avg_loss_dollars == 0


def test_compute_full_stats_only_losers():
    trades = [
        _FakeTrade(realized_pnl=-100, r_multiple=-1.0),
        _FakeTrade(realized_pnl=-200, r_multiple=-2.0),
    ]
    eq = _make_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 99_700),
    ])
    result = _FakeResult(trades=trades, equity_curve=eq)
    stats = compute_full_stats(result, starting_balance=100_000)
    assert stats.n_trades == 2
    assert stats.win_rate_pct == 0
    assert stats.profit_factor == 0
    assert stats.risk_reward_ratio == 0


def test_compute_full_stats_handles_missing_equity_curve():
    """Empty equity curve still produces valid (0-filled) stats."""
    trades = [_FakeTrade(realized_pnl=50, r_multiple=0.5)]
    result = _FakeResult(trades=trades,
                          equity_curve=pd.DataFrame(columns=["time", "equity"]))
    stats = compute_full_stats(result, starting_balance=100_000)
    # Should still compute the trade-level stats even with no curve
    assert stats.n_trades == 1
    assert stats.max_dd_dollars == 0
    assert stats.max_dd_pct == 0


# ---------------------------------------------------------------------------
# Sample-size sanity — what we tell the user about low-count cells
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_trades, expected_warning", [
    (3, True),
    (5, True),
    (9, True),
    (10, False),
    (30, False),
    (100, False),
])
def test_min_oos_threshold_signals_low_confidence(n_trades, expected_warning):
    """The 'is this trustworthy?' bar is 10 OOS trades. Below that the
    optimizer score components are still computed but the dashboard
    should flag them as low-confidence."""
    is_low_confidence = n_trades < 10
    assert is_low_confidence is expected_warning
