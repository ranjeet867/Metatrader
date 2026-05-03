"""
test_ftmo_simulator.py — INVARIANT-7 (seeded RNG) + sanity bounds.
"""
from __future__ import annotations

import numpy as np

from core.ftmo_simulator import StrategyDist, simulate_pass_rate


def test_seeded_rng_is_deterministic():
    """Same inputs + same seed → same output."""
    pool = StrategyDist(name="x", symbol="X",
                          r_multiples=np.array([1.0, -1.0, 2.0, -1.0, 0.5]),
                          trades_per_day=0.5, risk_per_trade_pct=1.0)
    a = simulate_pass_rate([pool], n_iterations=500, seed=42)
    b = simulate_pass_rate([pool], n_iterations=500, seed=42)
    assert a.p_pass == b.p_pass
    assert a.expected_final_pct == b.expected_final_pct
    np.testing.assert_array_equal(a.raw_finals, b.raw_finals)


def test_winning_pool_has_higher_pass_rate_than_losing():
    """A winning OOS distribution should pass more often than a losing one."""
    winning = StrategyDist(name="winner", symbol="X",
                              r_multiples=np.array([+0.3] * 100),
                              trades_per_day=1.0, risk_per_trade_pct=1.0)
    losing = StrategyDist(name="loser", symbol="X",
                             r_multiples=np.array([-0.2] * 100),
                             trades_per_day=1.0, risk_per_trade_pct=1.0)
    a = simulate_pass_rate([winning], n_iterations=500, seed=42)
    b = simulate_pass_rate([losing], n_iterations=500, seed=42)
    assert a.p_pass > b.p_pass


def test_empty_portfolio_returns_zero_pass():
    res = simulate_pass_rate([], n_iterations=100, seed=42)
    assert res.p_pass == 0.0


def test_probabilities_sum_to_at_most_one():
    pool = StrategyDist(name="x", symbol="X",
                          r_multiples=np.array([0.0, 1.0, -1.0]),
                          trades_per_day=0.5, risk_per_trade_pct=1.0)
    res = simulate_pass_rate([pool], n_iterations=300, seed=1)
    total = res.p_pass + res.p_daily_breach + res.p_total_breach + res.p_no_resolution
    assert abs(total - 1.0) < 1e-9


def test_contributions_per_strategy_present():
    a = StrategyDist(name="A", symbol="A", r_multiples=np.array([0.5]),
                       trades_per_day=0.5, risk_per_trade_pct=1.0)
    b = StrategyDist(name="B", symbol="B", r_multiples=np.array([0.5]),
                       trades_per_day=0.5, risk_per_trade_pct=1.0)
    res = simulate_pass_rate([a, b], n_iterations=200, seed=1)
    assert set(res.contrib_R_per_strategy.keys()) == {"A", "B"}
    assert set(res.contrib_dd_per_strategy.keys()) == {"A", "B"}
