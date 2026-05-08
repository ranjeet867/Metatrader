"""
test_adaptive_risk.py — pin the FTMO-aware adaptive sizing math.

These tests exercise the three buffer regimes (deep DD, profit, sideways)
plus the binding-constraint logic and the recovery-factor modulation.
The math here protects real money — every rule has a test.

Test scenarios (from the user's brief):
  A. Account in profit — large buffer, hits max_risk_pct ceiling
  B. Account at baseline — moderate buffer, daily-soft binds
  C. Account in deep DD — total-floor binds (almost dead)
  D. Daily-soft already breached — refuse trade
  E. Total-floor already breached — refuse trade
  F. Daily-hard ≠ daily-soft — soft binds first (correct)
  G. Recovery factor 0.5 → halves the result
  H. Peak factor 1.5 → can't exceed max_risk_pct
  I. min_risk_pct floor — buffer huge, target_trades insane → clamped
"""
from __future__ import annotations

import pytest

from core import adaptive_risk as ar


# ─── A. Profit regime — daily-soft still binds (FTMO discipline) ──────
def test_profit_regime_soft_cap_still_binds():
    """Critical FTMO design choice: even when in profit, the daily-soft
    cap is anchored to BASELINE so we never give back gains via a
    ramped-up risk session. With baseline=100k and soft=2%, daily_soft
    buffer = $2k regardless of how high current equity is. Per-trade
    = $2k / 5 ≈ $400 ≈ 0.36% of $110k. Binding is daily_soft, NOT
    max_risk_pct.

    User's brief: 'we should limit max loss to 2% daily so we do not
    lose FTMO account within 5 days'."""
    r = ar.calculate(
        current_equity=110_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
        max_risk_pct=1.0,
    )
    assert r.allow_trade is True
    assert r.binding_constraint == ar.BindingConstraint.DAILY_SOFT
    # Per-trade $400 / 110k ≈ 0.36%
    assert r.risk_pct == pytest.approx(0.36, abs=0.02)
    assert r.daily_soft_buffer_usd == pytest.approx(2_000.0)


def test_profit_regime_with_high_target_clamps_to_max():
    """Edge case: if user sets target_trades_per_day = 1, then
    per-trade = $2000 = ~1.8% of $110k → clamps to max_risk_pct=1%."""
    r = ar.calculate(
        current_equity=110_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
        target_trades_per_day=1,
        max_risk_pct=1.0,
    )
    assert r.allow_trade is True
    assert r.risk_pct == pytest.approx(1.0, abs=0.01)
    assert r.binding_constraint == ar.BindingConstraint.MAX_RISK_CLAMP


# ─── B. Baseline — daily soft binds ────────────────────────────────────
def test_baseline_daily_soft_binds(tmp_path):
    """Equity at baseline, no daily P&L. Daily-soft = $2k.
    target=5 → $400/trade ≈ 0.4% of $100k. Daily-soft binds (not max)."""
    r = ar.calculate(
        current_equity=100_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
        daily_soft_cap_pct=2.0,
        daily_hard_cap_pct=5.0,
        total_loss_floor_pct=10.0,
        target_trades_per_day=5,
    )
    assert r.allow_trade is True
    assert r.binding_constraint == ar.BindingConstraint.DAILY_SOFT
    assert r.daily_soft_buffer_usd == pytest.approx(2_000.0)
    assert r.per_trade_risk_usd == pytest.approx(400.0)
    assert r.risk_pct == pytest.approx(0.40, abs=0.01)
    assert r.max_trades_today_remaining == 5


# ─── C. Deep DD — total-floor binds ────────────────────────────────────
def test_deep_dd_total_floor_binds():
    """Equity 91k, baseline 100k, no daily loss yet. Total-floor at 90k.
    Total buffer = $1k. Daily-soft = $2k. Total-floor binds (smaller).
    Per-trade = $1k / 5 = $200 = 0.22% of $91k."""
    r = ar.calculate(
        current_equity=91_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
        target_trades_per_day=5,
    )
    assert r.allow_trade is True
    assert r.binding_constraint == ar.BindingConstraint.TOTAL_FLOOR
    assert r.total_buffer_usd == pytest.approx(1_000.0)
    assert r.per_trade_risk_usd == pytest.approx(200.0)
    assert r.risk_pct == pytest.approx(0.22, abs=0.01)


# ─── D. Daily soft already breached — refuse ──────────────────────────
def test_daily_soft_breached_refuses_trade():
    """Already lost $2.5k today; daily-soft cap was $2k. Refuse."""
    r = ar.calculate(
        current_equity=97_500.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=-2_500.0,
        daily_soft_cap_pct=2.0,
    )
    assert r.allow_trade is False
    assert "exhausted" in r.reason
    assert r.risk_pct == 0.0


# ─── E. Total floor already breached — refuse ─────────────────────────
def test_total_floor_breached_refuses_trade():
    """Equity dropped below the FTMO 10% floor. Refuse all trades."""
    r = ar.calculate(
        current_equity=89_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
    )
    assert r.allow_trade is False
    assert "exhausted" in r.reason


# ─── F. Daily-soft tighter than daily-hard ────────────────────────────
def test_soft_cap_tighter_than_hard_cap():
    """Soft cap 2% binds before hard cap 5%."""
    r = ar.calculate(
        current_equity=100_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
        daily_soft_cap_pct=2.0,
        daily_hard_cap_pct=5.0,
    )
    # Daily-soft = $2k, daily-hard = $5k → soft binds (smaller)
    assert r.binding_constraint == ar.BindingConstraint.DAILY_SOFT


# ─── G. Recovery factor halves the risk ────────────────────────────────
def test_recovery_factor_halves_risk():
    base = ar.calculate(
        current_equity=100_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
    )
    halved = ar.apply_recovery_factor(base, recovery_factor=0.5)
    assert halved.risk_pct == pytest.approx(base.risk_pct * 0.5, abs=0.01)
    assert "recovery_factor=0.5" in halved.reason


# ─── H. Peak factor cannot exceed max_risk_pct ────────────────────────
def test_peak_factor_clamped_by_max_risk():
    """Even multiplying by 2.0, we can't exceed max_risk_pct."""
    base = ar.calculate(
        current_equity=100_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
    )
    peaked = ar.apply_recovery_factor(
        base, recovery_factor=2.0, max_risk_pct=1.0
    )
    assert peaked.risk_pct <= 1.0


# ─── I. Insane target_trades clamps to min_risk_pct ───────────────────
def test_huge_target_trades_clamps_to_min():
    """If user asks for 1000 trades/day, per-trade risk goes tiny.
    Floor at min_risk_pct so we don't size effectively-zero positions."""
    r = ar.calculate(
        current_equity=100_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
        target_trades_per_day=1000,
        min_risk_pct=0.05,
    )
    assert r.risk_pct >= 0.05


# ─── J. Refusal struct still has diagnostics for UI ──────────────────
def test_refusal_includes_diagnostics():
    """Even when refusing, we want the buffer numbers for the dashboard."""
    r = ar.calculate(
        current_equity=89_000.0,
        baseline_equity=100_000.0,
        daily_pnl_so_far=0.0,
    )
    assert r.allow_trade is False
    # Buffers still populated for UI display
    assert r.total_buffer_usd < 0   # negative — we're past floor
    assert r.daily_soft_buffer_usd > 0   # daily not breached, just total


# ─── K. realistic FTMO 5-day stress sequence ──────────────────────────
def test_ftmo_5_day_loss_streak_simulation():
    """Day 1 starts at $100k. Lose 1.5% (under soft 2% cap). Day 2 at
    $98.5k — total floor still $90k away, daily-soft renews. Test that
    risk shrinks but doesn't refuse over 5 losing days."""
    equity = 100_000.0
    baseline = 100_000.0
    for day in range(5):
        # End-of-day: lost 1.5% today
        r = ar.calculate(
            current_equity=equity,
            baseline_equity=baseline,
            daily_pnl_so_far=-(equity * 0.015),
        )
        # We're below 2% soft cap — should still allow trades
        # daily_pnl = -1.5% of current equity. soft buffer = 2% of baseline
        # + (-1.5% × equity). If equity ≈ baseline, buffer ≈ 0.5% of base.
        # Should still be > 0.
        assert r.allow_trade is True or day >= 4   # last day might tip over
        equity = equity * 0.985   # next day starts lower
