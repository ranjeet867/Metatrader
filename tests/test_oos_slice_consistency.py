"""
test_oos_slice_consistency.py — pin the slice_result_to_oos helper.

Bug history: optimizer + Backtest sweep table showed maxDD%/DDdays/
recovD computed on FULL equity_curve while win%/netPnL$ used OOS-only
trades. Result: nonsensical rows where positive net P&L but recovery
days = None (because full curve never recovered all-time peak even
though OOS curve did).

These tests pin:
  • slicing returns OOS-only trades AND time-filtered equity_curve
  • rebasing makes the OOS curve start at the requested baseline
  • compute_full_stats then produces internally-consistent metrics
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pandas as pd
import pytest

from core.backtest_stats import compute_full_stats, slice_result_to_oos


@dataclass
class _FakeTrade:
    entry_bar_idx: int
    exit_bar_idx: int
    realized_pnl: float
    r_multiple: float


@dataclass
class _FakeResult:
    trades: List[_FakeTrade]
    equity_curve: pd.DataFrame
    starting_balance: float = 100_000.0


def _eq_curve(time_pnl_pairs):
    return pd.DataFrame(
        [{"time": t, "equity": e} for t, e in time_pnl_pairs]
    )


def _candles(times):
    return pd.DataFrame({"time": times})


def test_slice_returns_only_oos_trades():
    candles = _candles([f"2025-01-{i:02d}" for i in range(1, 11)])
    eq = _eq_curve(list(zip([f"2025-01-{i:02d}" for i in range(1, 11)],
                              [100_000 + i*100 for i in range(10)])))
    trades = [
        _FakeTrade(entry_bar_idx=1, exit_bar_idx=2, realized_pnl=50, r_multiple=0.5),
        _FakeTrade(entry_bar_idx=3, exit_bar_idx=4, realized_pnl=-30, r_multiple=-0.3),
        _FakeTrade(entry_bar_idx=7, exit_bar_idx=8, realized_pnl=200, r_multiple=2.0),
        _FakeTrade(entry_bar_idx=8, exit_bar_idx=9, realized_pnl=80, r_multiple=0.8),
    ]
    r = _FakeResult(trades=trades, equity_curve=eq)
    sliced = slice_result_to_oos(r, split_bar_idx=6, candles=candles)
    assert len(sliced.trades) == 2
    assert all(t.entry_bar_idx >= 6 for t in sliced.trades)


def test_slice_filters_equity_curve_by_time():
    candles = _candles([f"2025-01-{i:02d}" for i in range(1, 11)])
    eq = _eq_curve(list(zip([f"2025-01-{i:02d}" for i in range(1, 11)],
                              [100_000 + i*100 for i in range(10)])))
    r = _FakeResult(trades=[], equity_curve=eq)
    sliced = slice_result_to_oos(r, split_bar_idx=6, candles=candles)
    # Should only have entries from 2025-01-07 onward (4 entries: 7,8,9,10)
    assert len(sliced.equity_curve) == 4
    assert sliced.equity_curve["time"].min() >= pd.Timestamp("2025-01-07", tz="UTC")


def test_rebase_makes_oos_curve_start_at_target():
    candles = _candles([f"2025-01-{i:02d}" for i in range(1, 11)])
    # Train: 100k → 110k by day 6
    # OOS: 110k → 120k by day 10
    eq = _eq_curve([
        ("2025-01-01", 100_000),
        ("2025-01-02", 102_000),
        ("2025-01-03", 105_000),
        ("2025-01-04", 108_000),
        ("2025-01-05", 109_000),
        ("2025-01-06", 110_000),  # split point
        ("2025-01-07", 112_000),
        ("2025-01-08", 115_000),
        ("2025-01-09", 118_000),
        ("2025-01-10", 120_000),
    ])
    r = _FakeResult(trades=[], equity_curve=eq)
    sliced = slice_result_to_oos(r, split_bar_idx=6, candles=candles,
                                    rebase_to=100_000)
    # OOS first entry should be exactly 100k after rebase
    assert sliced.equity_curve["equity"].iloc[0] == 100_000
    # OOS last entry: original 120k - offset 12k = 108k
    assert sliced.equity_curve["equity"].iloc[-1] == 108_000


def test_oos_stats_are_internally_consistent_after_slice():
    """The bug we're fixing: positive net OOS P&L but recovery_days=None
    because full curve never recovered. After slicing properly, recovery
    is computed only over OOS, so a positive-OOS strategy should not
    say 'never recovered'."""
    # Setup: Train period peaks at 130k then drops to 95k. OOS recovers
    # to 105k (positive OOS P&L, but never reached full-peak 130k).
    candles = _candles([
        "2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01",  # train
        "2025-05-01", "2025-06-01", "2025-07-01", "2025-08-01",  # split + OOS
    ])
    eq = _eq_curve([
        ("2025-01-01", 100_000),
        ("2025-02-01", 130_000),    # train peak
        ("2025-03-01", 110_000),
        ("2025-04-01", 95_000),     # train trough — at split boundary
        ("2025-05-01", 95_000),     # OOS start
        ("2025-06-01", 90_000),     # OOS dip
        ("2025-07-01", 100_000),
        ("2025-08-01", 105_000),    # OOS end — recovered above OOS start
    ])
    trades = [
        # Train trades irrelevant — only counts for slicing
        _FakeTrade(entry_bar_idx=0, exit_bar_idx=1, realized_pnl=30_000, r_multiple=3),
        _FakeTrade(entry_bar_idx=1, exit_bar_idx=3, realized_pnl=-35_000, r_multiple=-3.5),
        # OOS trades (entry_bar_idx >= 4)
        _FakeTrade(entry_bar_idx=4, exit_bar_idx=5, realized_pnl=-5_000, r_multiple=-1),
        _FakeTrade(entry_bar_idx=5, exit_bar_idx=6, realized_pnl=10_000, r_multiple=2),
        _FakeTrade(entry_bar_idx=6, exit_bar_idx=7, realized_pnl=5_000, r_multiple=1),
    ]
    r = _FakeResult(trades=trades, equity_curve=eq)
    # SLICE: keep only OOS trades + OOS equity, rebase to 100k
    sliced = slice_result_to_oos(r, split_bar_idx=4, candles=candles,
                                    rebase_to=100_000)
    stats = compute_full_stats(sliced, starting_balance=100_000)

    # OOS-only counts
    assert stats.n_trades == 3
    assert stats.n_wins == 2
    assert stats.n_losses == 1
    # Net P&L = -5000 + 10000 + 5000 = 10000 (positive)
    assert stats.sum_realized == 10_000
    # OOS curve rebased: 100k → 95k (dip) → 105k → 110k (recovered above start)
    # Max DD: 100k → 95k = $5,000 / 5%
    assert abs(stats.max_dd_dollars - 5_000) < 1
    # Recovery days should NOT be None — OOS recovered after the dip
    assert stats.recovery_duration_days is not None, (
        "Bug pinned: positive OOS P&L should produce a recovery_days value, "
        "not None. Previously equity_curve wasn't sliced to OOS so recovery "
        "was computed against full-history peak (130k) which OOS never reached."
    )


def test_short_oos_slice_returns_empty_curve_safely():
    """Edge: split point is past the data. No crash, returns the
    original result unmodified."""
    candles = _candles(["2025-01-01", "2025-01-02"])
    eq = _eq_curve([("2025-01-01", 100_000), ("2025-01-02", 101_000)])
    r = _FakeResult(trades=[], equity_curve=eq)
    out = slice_result_to_oos(r, split_bar_idx=999, candles=candles)
    # No-op when split is beyond data
    assert out is r or len(out.equity_curve) == len(eq)
