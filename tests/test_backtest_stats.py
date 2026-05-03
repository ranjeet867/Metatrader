"""
test_backtest_stats.py — every metric in core.backtest_stats.FullStats
verified against synthetic trades + equity curves with known answers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core import backtest_stats


# --- minimal fakes that quack like ClosedTrade / BacktestResult ------------

@dataclass
class FakeTrade:
    realized_pnl: float
    r_multiple: float
    entry_bar_idx: int = 0
    exit_bar_idx: int = 1


@dataclass
class FakeResult:
    trades: list
    equity_curve: pd.DataFrame
    starting_balance: float
    ending_balance: float = 0.0
    sum_realized_pnl: float = 0.0
    equity_curve_pnl: float = 0.0
    reconciles: bool = True


def _eq_curve(values: list[float], *, start: datetime = None
              ) -> pd.DataFrame:
    """Build a simple per-day equity curve from a list of equity values."""
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"time": start + timedelta(days=i),
             "equity": float(v)}
            for i, v in enumerate(values)]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Headline counts
# ---------------------------------------------------------------------------

def test_no_trades():
    res = FakeResult(trades=[], equity_curve=pd.DataFrame(),
                      starting_balance=100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.n_trades == 0
    assert s.win_rate_pct == 0
    assert s.profit_factor == 0
    assert s.expectancy_dollars == 0
    assert s.avg_R == 0
    assert s.max_dd_dollars == 0
    assert s.max_consec_wins == 0
    assert s.max_consec_losses == 0


def test_win_rate_and_counts():
    trades = [
        FakeTrade(100, 1.0), FakeTrade(-50, -1.0), FakeTrade(200, 2.0),
        FakeTrade(-50, -1.0), FakeTrade(50, 0.5),
    ]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.n_trades == 5
    assert s.n_wins == 3
    assert s.n_losses == 2
    assert s.win_rate_pct == 60.0


# ---------------------------------------------------------------------------
# Profit factor / expectancy / avg R
# ---------------------------------------------------------------------------

def test_profit_factor():
    trades = [FakeTrade(300, 1.5), FakeTrade(-100, -0.5),
                FakeTrade(-50, -0.25)]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.profit_factor == pytest.approx(2.0)   # 300 / 150


def test_profit_factor_inf_when_no_losses():
    trades = [FakeTrade(100, 1.0), FakeTrade(50, 0.5)]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.profit_factor == float("inf")


def test_expectancy_and_avg_R():
    trades = [FakeTrade(100, 1.0), FakeTrade(-50, -1.0),
                FakeTrade(150, 1.5)]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.expectancy_dollars == pytest.approx(200 / 3)
    assert s.avg_R == pytest.approx(1.5 / 3)


# ---------------------------------------------------------------------------
# Win / loss anatomy + R:R
# ---------------------------------------------------------------------------

def test_avg_win_avg_loss_and_rr():
    trades = [FakeTrade(200, 2.0), FakeTrade(-100, -1.0),
                FakeTrade(300, 3.0), FakeTrade(-50, -0.5)]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.avg_win_dollars == pytest.approx(250.0)
    assert s.avg_loss_dollars == pytest.approx(-75.0)
    assert s.risk_reward_ratio == pytest.approx(250 / 75)


def test_largest_win_and_loss():
    trades = [FakeTrade(50, 0.5), FakeTrade(500, 5.0),
                FakeTrade(-200, -2.0), FakeTrade(-30, -0.3)]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.largest_win_dollars == 500
    assert s.largest_loss_dollars == -200


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

def test_max_consec_wins():
    pnls = [100, 50, 80, -20, 30, 40, 50, 60, -10]
    trades = [FakeTrade(p, p / 100) for p in pnls]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    # Best streak is 4 wins (30, 40, 50, 60)
    assert s.max_consec_wins == 4


def test_max_consec_losses():
    pnls = [100, -10, -20, -30, -5, 50, -10, -10]
    trades = [FakeTrade(p, p / 100) for p in pnls]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.max_consec_losses == 4


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def test_drawdown_simple():
    """Equity peaks at 110k, troughs at 95k, recovers to 115k.
    Max DD should be $15k (≈13.6% of $110k peak)."""
    eq = _eq_curve([100_000, 105_000, 110_000, 100_000, 95_000,
                     100_000, 115_000])
    res = FakeResult([], eq, 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.max_dd_dollars == 15_000
    assert s.max_dd_pct == pytest.approx(15_000 / 110_000 * 100, rel=1e-3)
    # Peak at day 2, trough at day 4 → 2 days
    assert s.max_dd_duration_days == pytest.approx(2.0)
    # Recovery: trough day 4, recovers to ≥110k on day 6 → 2 days
    assert s.recovery_duration_days == pytest.approx(2.0)


def test_drawdown_no_recovery_yet():
    """Curve ends at the trough — recovery duration is None."""
    eq = _eq_curve([100_000, 110_000, 105_000, 95_000])
    res = FakeResult([], eq, 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.max_dd_dollars == 15_000
    assert s.recovery_duration_days is None


def test_drawdown_zero_when_only_up():
    eq = _eq_curve([100_000, 102_000, 104_000, 108_000])
    res = FakeResult([], eq, 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.max_dd_dollars == 0
    assert s.max_dd_pct == 0


# ---------------------------------------------------------------------------
# CAGR / span
# ---------------------------------------------------------------------------

def test_cagr_one_year_double():
    """100k → 200k over exactly 365.25 days → CAGR = 100%."""
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    eq = pd.DataFrame([
        {"time": start, "equity": 100_000.0},
        {"time": start + timedelta(days=365.25), "equity": 200_000.0},
    ])
    res = FakeResult([], eq, 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.cagr_pct == pytest.approx(100.0, abs=0.5)
    assert s.span_days == pytest.approx(365.25, abs=0.1)


def test_cagr_short_span_returns_none():
    eq = _eq_curve([100_000, 105_000, 110_000])    # 3 days only
    res = FakeResult([], eq, 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.cagr_pct is None


# ---------------------------------------------------------------------------
# Sharpe-R
# ---------------------------------------------------------------------------

def test_sharpe_R_zero_for_single_trade():
    res = FakeResult([FakeTrade(100, 1.0)], _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.sharpe_R == 0.0


def test_sharpe_R_positive_when_consistent_winners():
    trades = [FakeTrade(p, p / 100) for p in [50, 60, 55, 65, 50]]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    assert s.sharpe_R > 5    # very high when std is tiny


# ---------------------------------------------------------------------------
# Rescaling helper
# ---------------------------------------------------------------------------

def test_rescale_preserves_proportional_pnl():
    eq = _eq_curve([91_400, 92_400, 90_400, 95_400])
    res = FakeResult([], eq, 91_400)
    out = backtest_stats.rescale_to_starting_balance(res, 100_000)
    # Starts at exactly 100_000
    assert out["equity"].iloc[0] == pytest.approx(100_000)
    # Final return is +(95400-91400)/91400 = +4.376%, which becomes
    # 100000 × 1.04376 = 104,376
    expected_end = 100_000 * (95_400 / 91_400)
    assert out["equity"].iloc[-1] == pytest.approx(expected_end, rel=1e-4)


def test_rescale_handles_empty():
    res = FakeResult([], pd.DataFrame(), 100_000)
    out = backtest_stats.rescale_to_starting_balance(res, 100_000)
    assert out is None or out.empty


# ---------------------------------------------------------------------------
# Trade pacing
# ---------------------------------------------------------------------------

def test_median_and_avg_trade_bars():
    trades = [
        FakeTrade(100, 1, entry_bar_idx=0, exit_bar_idx=2),
        FakeTrade(200, 2, entry_bar_idx=2, exit_bar_idx=10),
        FakeTrade(50, 0.5, entry_bar_idx=10, exit_bar_idx=14),
    ]
    res = FakeResult(trades, _eq_curve([100_000]), 100_000)
    s = backtest_stats.compute_full_stats(res, starting_balance=100_000)
    # Bars: 2, 8, 4 → median 4, avg ~4.67
    assert s.median_trade_bars == 4
    assert s.avg_trade_bars == pytest.approx(14 / 3)
