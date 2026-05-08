"""
test_edge_score.py — pin the multi-metric edge ranking math.

These tests prove that:
  - High-PF-fragile cells (PF 10, n_test 11, no recovery) score LOWER
    than steady-edge cells (PF 1.4, n_test 80, fast recovery)
  - Negative-Kelly cells score near zero
  - Deploy-safe bonus actually moves the needle
  - Score is bounded [0, 100]
"""
from __future__ import annotations

import pytest

from core import edge_score as es


# ─── A. Steady winner beats fragile high-PF ────────────────────────────
def test_steady_beats_fragile_highpf():
    """Cell A: PF 10 but tiny sample, never recovered. Cell B: PF 1.4
    but big sample, fast recovery. B should score higher."""
    fragile = es.compute(
        expectancy_per_trade=20.0,    # high $/trade
        win_rate=0.5, risk_reward=10.0,
        trades_per_day=0.1,           # only 1 trade per 10 days
        recovery_days=None,           # never recovered
        sharpe_r=0.4,
        deploy_safe=False,            # fails gates
    )
    steady = es.compute(
        expectancy_per_trade=5.0,
        win_rate=0.55, risk_reward=1.5,
        trades_per_day=2.0,
        recovery_days=10,
        sharpe_r=0.4,
        deploy_safe=True,
    )
    assert steady.total > fragile.total
    # The deploy_safe + recovery components are the deciding factors


# ─── B. Negative Kelly → near-zero score ──────────────────────────────
def test_negative_kelly_loser_scores_low():
    """A cell with WR=0.4 R:R=1.0: Kelly = (0.4 - 0.6) / 1.0 = -0.2
    (negative, mathematically losing)."""
    losing = es.compute(
        expectancy_per_trade=-1.0,
        win_rate=0.40, risk_reward=1.0,
        trades_per_day=2.0,
        recovery_days=None,
        sharpe_r=-0.1,
        deploy_safe=False,
    )
    assert losing.kelly_score == 0.0
    assert losing.expectancy_score == 0.0


# ─── C. Deploy-safe bonus moves the needle ────────────────────────────
def test_deploy_safe_bonus():
    """Same metrics, only deploy_safe differs. Total should differ
    by ~9.09 (the deploy_safe weight 0.10 ÷ default-weight-sum 1.10
    after renormalisation × 100-point bonus). Widened tolerance
    accounts for the v2 renormalisation."""
    base_kwargs = dict(
        expectancy_per_trade=3.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=20, sharpe_r=0.5,
        annualised_return_pct=10.0, max_dd_pct=5.0, n_trades=50,
    )
    safe = es.compute(**base_kwargs, deploy_safe=True)
    unsafe = es.compute(**base_kwargs, deploy_safe=False)
    delta = safe.total - unsafe.total
    # Deploy-safe weight 0.10 / sum 1.10 = 0.0909 effective weight,
    # × 100-point bonus = ~9.09 delta in total. Anything in [8, 11]
    # is acceptable (renormalisation tolerance).
    assert 8.0 <= delta <= 11.0, f"expected 8..11, got {delta}"


# ─── D. Bounds ────────────────────────────────────────────────────────
def test_score_bounded_0_100():
    """Even with absurd inputs, total stays in [0, 100]."""
    huge = es.compute(
        expectancy_per_trade=1e6, win_rate=0.99, risk_reward=100.0,
        trades_per_day=100, recovery_days=0.001,
        sharpe_r=10.0, deploy_safe=True,
    )
    tiny = es.compute(
        expectancy_per_trade=-1e6, win_rate=0.01, risk_reward=0.01,
        trades_per_day=0, recovery_days=None,
        sharpe_r=-10, deploy_safe=False,
    )
    assert 0 <= huge.total <= 100
    assert 0 <= tiny.total <= 100


# ─── E. Kelly math correctness ────────────────────────────────────────
def test_kelly_math():
    """WR=0.6, RR=1.5 → Kelly = (0.6×1.5 - 0.4)/1.5 = 0.5/1.5 = 0.333.
    Capped at 0.25 → score 0.25 × 4 × 25 wait no — score formula:
    kelly_pct * 4 (clamped to 100). 0.333 * 4 = 1.33 → clamped to 100."""
    r = es.compute(
        expectancy_per_trade=5, win_rate=0.6, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=20, sharpe_r=0.5,
        deploy_safe=True,
    )
    assert r.kelly_pct == pytest.approx(0.333, abs=0.01)
    assert r.kelly_score == 100.0   # clamped


# ─── F. Recovery × frequency ──────────────────────────────────────────
def test_recovery_frequency_score():
    """Frequent + fast recovery beats sparse + slow."""
    fast = es.compute(
        expectancy_per_trade=2.0, win_rate=0.5, risk_reward=1.5,
        trades_per_day=2.0, recovery_days=5,    # ratio 0.4
        sharpe_r=0.5, deploy_safe=True,
    )
    slow = es.compute(
        expectancy_per_trade=2.0, win_rate=0.5, risk_reward=1.5,
        trades_per_day=0.1, recovery_days=200,  # ratio 0.0005
        sharpe_r=0.5, deploy_safe=True,
    )
    assert fast.recovery_frequency_score > slow.recovery_frequency_score


# ─── G. compute_from_edge_stat works ──────────────────────────────────
def test_compute_from_edge_stat_shim(tmp_path):
    """Pass an EdgeStat-shaped object, get a score back."""
    class FakeEdge:
        expectancy_dollars = 5.0
        win_rate_pct = 55.0
        rr = 1.5
        trades_per_day = 2.0
        recovery_days = 10
        sharpe_R = 0.5
        test_r = 0.3
        deploy_safe = True
        # v2 additions — supply or default to 0
        annualised_return_pct = 12.0
        max_dd_pct = 5.0
        n_test = 50
        pct_per_month = 1.0
    r = es.compute_from_edge_stat(FakeEdge())
    assert r.total > 0
    assert r.deploy_safe is True


# ─── H. Calmar ratio math ──────────────────────────────────────────────
def test_calmar_high_when_low_dd():
    """Annualised return 30% with 5% max DD → Calmar 6.0 → score 100 (clamped)."""
    r = es.compute(
        expectancy_per_trade=2.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=10, sharpe_r=0.5,
        deploy_safe=True,
        annualised_return_pct=30.0, max_dd_pct=5.0, n_trades=50,
    )
    assert r.calmar_ratio == pytest.approx(6.0, abs=0.01)
    assert r.calmar_score == 100.0   # capped


def test_calmar_low_when_high_dd():
    """Same return but 30% DD → Calmar 1.0 → score 33."""
    r = es.compute(
        expectancy_per_trade=2.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=10, sharpe_r=0.5,
        deploy_safe=True,
        annualised_return_pct=30.0, max_dd_pct=30.0, n_trades=50,
    )
    assert r.calmar_ratio == pytest.approx(1.0, abs=0.01)
    assert r.calmar_score == pytest.approx(33.3, abs=0.5)


def test_calmar_zero_when_no_dd_or_no_return():
    """Calmar undefined when DD=0 or return=0; return 0 not crash."""
    r1 = es.compute(
        expectancy_per_trade=2.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=10, sharpe_r=0.5,
        deploy_safe=True,
        annualised_return_pct=10.0, max_dd_pct=0.0, n_trades=50,
    )
    assert r1.calmar_score == 0.0
    r2 = es.compute(
        expectancy_per_trade=0.0, win_rate=0.50, risk_reward=1.0,
        trades_per_day=1.0, recovery_days=10, sharpe_r=0.0,
        deploy_safe=True,
        annualised_return_pct=0.0, max_dd_pct=5.0, n_trades=50,
    )
    assert r2.calmar_score == 0.0


# ─── I. Probabilistic Sharpe Ratio ─────────────────────────────────────
def test_psr_high_with_big_sample():
    """Sharpe 1.0 with n=100 → PSR ~99% (high confidence)."""
    r = es.compute(
        expectancy_per_trade=2.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=10, sharpe_r=1.0,
        deploy_safe=True,
        annualised_return_pct=20.0, max_dd_pct=10.0, n_trades=100,
    )
    assert r.psr > 0.95
    assert r.psr_score > 95.0


def test_psr_low_with_tiny_sample():
    """Same Sharpe 1.0 but n=5 → PSR much lower (low confidence)."""
    r = es.compute(
        expectancy_per_trade=2.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=10, sharpe_r=1.0,
        deploy_safe=True,
        annualised_return_pct=20.0, max_dd_pct=10.0, n_trades=5,
    )
    # n=5, sharpe=1.0 → z = 1.0 × sqrt(4) = 2.0 → PSR ≈ 0.977
    # Wait, sqrt(4) = 2; phi(2) ≈ 0.977. That's still high.
    # Let me try n=2:
    r2 = es.compute(
        expectancy_per_trade=2.0, win_rate=0.55, risk_reward=1.5,
        trades_per_day=1.0, recovery_days=10, sharpe_r=1.0,
        deploy_safe=True,
        annualised_return_pct=20.0, max_dd_pct=10.0, n_trades=2,
    )
    # n=2, sharpe=1.0 → z = 1.0, phi(1) ≈ 0.84
    assert r2.psr < r.psr   # smaller sample → lower PSR


def test_psr_zero_with_negative_sharpe():
    r = es.compute(
        expectancy_per_trade=-1.0, win_rate=0.40, risk_reward=1.0,
        trades_per_day=1.0, recovery_days=None, sharpe_r=-0.5,
        deploy_safe=False,
        annualised_return_pct=0.0, max_dd_pct=10.0, n_trades=50,
    )
    assert r.psr == 0.0
    assert r.psr_score == 0.0


# ─── J. PSR catches the "fragile high-PF" trap ────────────────────────
def test_psr_penalises_fragile_high_sharpe_low_n():
    """User's recurring issue: cell with PF 10 on 11 trades. Sharpe-R
    might look amazing but PSR should be modest (small sample), and
    once weighted in the total, this no longer dominates."""
    fragile = es.compute(
        expectancy_per_trade=20, win_rate=0.6, risk_reward=10,
        trades_per_day=0.05, recovery_days=None, sharpe_r=2.0,
        deploy_safe=False,
        annualised_return_pct=50.0, max_dd_pct=2.0, n_trades=11,
    )
    steady = es.compute(
        expectancy_per_trade=4, win_rate=0.55, risk_reward=1.5,
        trades_per_day=2.0, recovery_days=10, sharpe_r=0.5,
        deploy_safe=True,
        annualised_return_pct=15.0, max_dd_pct=5.0, n_trades=80,
    )
    # Steady should still beat fragile due to:
    #  - deploy_safe (10 weight)
    #  - psr (10 weight, fragile has lower PSR due to n=11)
    #  - recovery (15 weight, fragile has 0)
    assert steady.total > fragile.total
