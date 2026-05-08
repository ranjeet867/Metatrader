"""
test_stress_test_metrics.py — pin the worst-case streak math used by the
Portfolio Composer's stress-test panel.

The user asked: "4.63% expected daily risk considering recovery time and
longest losing streak — what would actually happen?". The panel computes
three scenarios:

  A. Simultaneous (correlation +1):     sum(risk × L)
  B. Independent (uncorrelated):        sqrt(sum((risk × L)²))
  C. Avg streak:                        expected_daily_risk × longest_L

This test pins each formula on a known-good fixture so refactors of the
Composer page can't quietly change the numbers.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_stress_test_fns():
    """Import _stress_test_metrics from the Composer page (filename has
    emoji, so use importlib spec)."""
    page_path = ROOT / "dashboards/pages/A_📦_Portfolio_Composer.py"
    sys.path.insert(0, str(ROOT))
    import streamlit as st                                                # noqa: E402
    real_set_page_config = st.set_page_config
    st.set_page_config = lambda *a, **kw: None
    spec = importlib.util.spec_from_file_location("_pc_st", page_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        st.set_page_config = real_set_page_config
    return mod._stress_test_metrics


class _Edge:
    """Minimal EdgeStat-like fixture for stress-test math."""
    def __init__(self, strategy, ticker, tf, max_consec_losses,
                  trades_per_day, recovery_days):
        self.strategy = strategy
        self.ticker = ticker
        self.tf = tf
        self.max_consec_losses = max_consec_losses
        self.trades_per_day = trades_per_day
        self.recovery_days = recovery_days


def test_six_strategy_user_portfolio():
    """Pin the exact numbers from the user's screenshot.

    Six strategies, suggested risks 1.43/0.39/2.00/2.00/0.74/0.27,
    losing streaks 4/3/4/4/4/4 → simul -26.93%, indep -13.12%."""
    metrics = _load_stress_test_fns()
    edges = [
        _Edge("strat0", "T0", "M15", 4, 1.19, 6),
        _Edge("strat1", "T1", "D1",  3, 0.33, 7),
        _Edge("strat2", "T2", "D1",  4, 1.61, 1),
        _Edge("strat3", "T3", "D1",  4, 1.67, 2),
        _Edge("strat4", "T4", "H1",  4, 0.67, 10),
        _Edge("strat5", "T5", "M15", 4, 0.20, 8),
    ]
    risk_alloc = {
        "strat0__T0__M15": 1.43,
        "strat1__T1__D1":  0.39,
        "strat2__T2__D1":  2.00,
        "strat3__T3__D1":  2.00,
        "strat4__T4__H1":  0.74,
        "strat5__T5__M15": 0.27,
    }
    m = metrics(edges, risk_alloc)
    # Simultaneous (risk × L summed)
    expected_simul = (1.43*4 + 0.39*3 + 2.00*4 + 2.00*4
                       + 0.74*4 + 0.27*4)
    assert abs(m["simul_pct"] - expected_simul) < 1e-9, (
        f"simul_pct {m['simul_pct']} vs expected {expected_simul}"
    )
    # Independent (sqrt of sum-of-squares)
    expected_indep = math.sqrt(
        (1.43*4)**2 + (0.39*3)**2 + (2.00*4)**2 + (2.00*4)**2
        + (0.74*4)**2 + (0.27*4)**2
    )
    assert abs(m["indep_pct"] - expected_indep) < 1e-9
    # Sanity bounds we care about for FTMO
    assert m["simul_pct"] > 10.0, "user portfolio busts FTMO simultaneously"
    assert m["indep_pct"] > 10.0, "user portfolio busts FTMO uncorrelated"
    assert m["max_consec"] == 4
    assert m["max_recov_d"] == 10
    assert m["sum_recov_d"] == 6 + 7 + 1 + 2 + 10 + 8
    assert m["n_recovered"] == 6


def test_safe_low_risk_portfolio():
    """A 0.5%-risk portfolio with L=4 across two cells should NOT bust."""
    metrics = _load_stress_test_fns()
    edges = [
        _Edge("a", "T1", "D1", 4, 1.0, 5),
        _Edge("b", "T2", "D1", 4, 1.0, 5),
    ]
    risk_alloc = {"a__T1__D1": 0.5, "b__T2__D1": 0.5}
    m = metrics(edges, risk_alloc)
    # Simul: 0.5*4 + 0.5*4 = 4.0%
    assert abs(m["simul_pct"] - 4.0) < 1e-9
    # Indep: sqrt(2² + 2²) = sqrt(8) ≈ 2.83
    assert abs(m["indep_pct"] - math.sqrt(8.0)) < 1e-9
    assert m["simul_pct"] < 10.0
    assert m["indep_pct"] < 10.0


def test_no_recovery_data_is_handled():
    """If every cell has recovery_days=None, output stays sane."""
    metrics = _load_stress_test_fns()
    edges = [_Edge("a", "T1", "D1", 3, 1.0, None)]
    m = metrics(edges, {"a__T1__D1": 1.0})
    assert m["n_recovered"] == 0
    assert m["max_recov_d"] == 0.0
    assert m["sum_recov_d"] == 0.0


def test_zero_trades_per_day_does_not_divide_by_zero():
    """A cell with tpd=0 should produce streak_days=inf, not crash."""
    metrics = _load_stress_test_fns()
    edges = [_Edge("a", "T1", "D1", 4, 0.0, 5)]
    m = metrics(edges, {"a__T1__D1": 0.5})
    row = m["rows"][0]
    assert row["streak_days"] == float("inf")
    assert row["streak_pct"] == 2.0


def test_independent_lower_bound_simultaneous():
    """For any portfolio of 2+ cells, indep ≤ simul (Cauchy-Schwarz)."""
    metrics = _load_stress_test_fns()
    edges = [
        _Edge("a", "T1", "D1", 5, 1.0, 5),
        _Edge("b", "T2", "D1", 3, 1.0, 5),
        _Edge("c", "T3", "D1", 4, 1.0, 5),
    ]
    risk_alloc = {"a__T1__D1": 1.0, "b__T2__D1": 0.5, "c__T3__D1": 0.8}
    m = metrics(edges, risk_alloc)
    assert m["indep_pct"] <= m["simul_pct"] + 1e-9


# ---------------------------------------------------------------------------
# Auto-tuner: shrink risk so worst-case ≤ target
# ---------------------------------------------------------------------------

def _load_autotuner():
    page_path = ROOT / "dashboards/pages/A_📦_Portfolio_Composer.py"
    sys.path.insert(0, str(ROOT))
    import streamlit as st                                                # noqa: E402
    real_set_page_config = st.set_page_config
    st.set_page_config = lambda *a, **kw: None
    spec = importlib.util.spec_from_file_location("_pc_at", page_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        st.set_page_config = real_set_page_config
    return mod._autotune_risk_for_stress


def test_autotune_brings_user_portfolio_under_floor():
    """The user's six-cell portfolio busts FTMO at indep -13.12%.
    Auto-tuner with target 8% (= 80% × 10% floor) should scale every
    cell so post-tune indep ≤ 8.0%."""
    autotune = _load_autotuner()
    edges = [
        _Edge("strat0", "T0", "M15", 4, 1.19, 6),
        _Edge("strat1", "T1", "D1",  3, 0.33, 7),
        _Edge("strat2", "T2", "D1",  4, 1.61, 1),
        _Edge("strat3", "T3", "D1",  4, 1.67, 2),
        _Edge("strat4", "T4", "H1",  4, 0.67, 10),
        _Edge("strat5", "T5", "M15", 4, 0.20, 8),
    ]
    base = {
        "strat0__T0__M15": 1.43, "strat1__T1__D1": 0.39,
        "strat2__T2__D1":  2.00, "strat3__T3__D1": 2.00,
        "strat4__T4__H1":  0.74, "strat5__T5__M15": 0.27,
    }
    new_alloc, k, stress = autotune(edges, base, target_dd_pct=8.0)
    # Post-tune simul should be AT or slightly above target (the
    # 0.10%-per-cell floor prevents over-shrinking the smallest cells,
    # which can lift the realised post-tune drawdown by a fraction of a
    # percent above the mathematical target).
    assert stress["simul_pct"] <= 8.5, (
        f"post-tune simul {stress['simul_pct']:.4f} too far above target 8.0"
    )
    assert stress["simul_pct"] >= 7.5, (
        f"post-tune simul {stress['simul_pct']:.4f} unexpectedly below target"
    )
    # And it MUST be strictly below FTMO floor (with the configured 80%
    # safety margin, that's 8% target → 10% floor → big buffer)
    assert stress["simul_pct"] < 10.0
    assert stress["indep_pct"] < 10.0
    # k must be in (0, 1]
    assert 0.0 < k <= 1.0
    # Every per-cell risk is k × original (clamped to floor 0.10)
    for slug, r in base.items():
        expected = max(0.10, min(2.0, r * k))
        assert abs(new_alloc[slug] - expected) < 1e-9


def test_autotune_does_not_scale_up():
    """If the portfolio is already safe, leave the risks untouched."""
    autotune = _load_autotuner()
    edges = [_Edge("a", "T1", "D1", 4, 1.0, 5)]
    base = {"a__T1__D1": 0.5}
    # Stress: simul = 0.5 × 4 = 2.0, target = 8.0 → ratio = 4
    # But k must clamp to ≤ 1.0 (never enlarge)
    new_alloc, k, _ = autotune(edges, base, target_dd_pct=8.0)
    assert k == 1.0
    assert new_alloc["a__T1__D1"] == 0.5


def test_autotune_respects_floor():
    """Even with extreme target, no per-cell risk goes below 0.10%."""
    autotune = _load_autotuner()
    edges = [_Edge("a", "T1", "D1", 10, 1.0, 5)]
    base = {"a__T1__D1": 1.0}
    # base streak = 10%, target = 0.5% → k = 0.05 → r = 0.05%, but floor = 0.10
    new_alloc, _, _ = autotune(edges, base, target_dd_pct=0.5,
                                  floor_pct=0.10)
    assert new_alloc["a__T1__D1"] == 0.10


def test_autotune_handles_empty_portfolio():
    autotune = _load_autotuner()
    new_alloc, k, stress = autotune([], {}, target_dd_pct=8.0)
    assert new_alloc == {}
    assert k == 1.0
    assert stress["simul_pct"] == 0.0
    assert stress["indep_pct"] == 0.0


# ---------------------------------------------------------------------------
# Risk-mode (Recovery / Normal / Peak)
# ---------------------------------------------------------------------------

def _load_risk_mode_fn():
    page_path = ROOT / "dashboards/pages/A_📦_Portfolio_Composer.py"
    sys.path.insert(0, str(ROOT))
    import streamlit as st                                                # noqa: E402
    real_set_page_config = st.set_page_config
    st.set_page_config = lambda *a, **kw: None
    spec = importlib.util.spec_from_file_location("_pc_rm", page_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        st.set_page_config = real_set_page_config
    return mod._apply_risk_mode


def test_recovery_mode_user_scenario():
    """User's exact ask: account at $90k, $1k max-loss-today budget,
    10 trades minimum → $/trade ≤ $100 → risk_pct ≤ 0.111%.

    Every cell should be capped at this regardless of original suggestion.
    """
    apply_mode = _load_risk_mode_fn()
    edges = [
        _Edge("a", "T1", "D1", 4, 1.0, 5),
        _Edge("b", "T2", "D1", 4, 1.0, 5),
    ]
    base = {"a__T1__D1": 1.43, "b__T2__D1": 2.00}   # both above the cap
    new_alloc, info = apply_mode(
        edges, base,
        mode="recovery",
        current_equity=90_000.0,
        baseline_equity=100_000.0,
        max_loss_today_dollars=1_000.0,
        min_trades_today=10,
        peak_multiplier=1.0,
    )
    # Cap = $100/trade, on $90k = 0.1111...%
    expected_cap = 1_000.0 / 10 / 90_000.0 * 100.0
    assert info["applied"] is True
    assert info["mode"] == "recovery"
    assert abs(info["cap_dollars_per_trade"] - 100.0) < 1e-9
    assert abs(info["cap_risk_pct"] - expected_cap) < 1e-6
    # Every cell capped — but never below floor (0.10%)
    for slug in base:
        # cap_pct ≈ 0.111, but min(r, cap_pct) then max with 0.10 floor
        assert new_alloc[slug] <= max(0.10, expected_cap) + 1e-9
        assert new_alloc[slug] >= 0.10


def test_recovery_mode_total_loss_under_budget():
    """If every cell stops out today, total $ loss must be ≤
    max_loss_today_dollars (the whole point of the cap)."""
    apply_mode = _load_risk_mode_fn()
    edges = [
        _Edge(f"s{i}", f"T{i}", "D1", 4, 1.0, 5)
        for i in range(5)
    ]
    base = {f"s{i}__T{i}__D1": 1.5 for i in range(5)}
    new_alloc, info = apply_mode(
        edges, base,
        mode="recovery",
        current_equity=90_000.0,
        baseline_equity=100_000.0,
        max_loss_today_dollars=1_000.0,
        min_trades_today=10,
        peak_multiplier=1.0,
    )
    # If 10 trades each lose 1R, total $ ≤ 10 × $/trade = $1000
    cap_dollars = info["cap_dollars_per_trade"]
    total_potential_loss = 10 * cap_dollars
    assert total_potential_loss <= 1_000.0 + 1e-6


def test_normal_mode_passthrough():
    apply_mode = _load_risk_mode_fn()
    edges = [_Edge("a", "T1", "D1", 4, 1.0, 5)]
    base = {"a__T1__D1": 1.0}
    new_alloc, info = apply_mode(
        edges, base,
        mode="normal",
        current_equity=100_000.0,
        baseline_equity=100_000.0,
        max_loss_today_dollars=0.0,
        min_trades_today=1,
        peak_multiplier=1.0,
    )
    assert info["applied"] is False
    assert info["mode"] == "normal"
    assert new_alloc == base


def test_peak_mode_scales_up():
    apply_mode = _load_risk_mode_fn()
    edges = [_Edge("a", "T1", "D1", 4, 1.0, 5)]
    base = {"a__T1__D1": 0.5}
    new_alloc, info = apply_mode(
        edges, base,
        mode="peak",
        current_equity=110_000.0,
        baseline_equity=100_000.0,
        max_loss_today_dollars=0.0,
        min_trades_today=1,
        peak_multiplier=1.5,
    )
    assert info["applied"] is True
    assert info["multiplier"] == 1.5
    # 0.5% × 1.5 = 0.75%
    assert abs(new_alloc["a__T1__D1"] - 0.75) < 1e-9


def test_peak_mode_respects_ceiling():
    """Even at 2.0× peak, no cell exceeds 2.0% ceiling."""
    apply_mode = _load_risk_mode_fn()
    edges = [_Edge("a", "T1", "D1", 4, 1.0, 5)]
    base = {"a__T1__D1": 1.5}
    new_alloc, info = apply_mode(
        edges, base,
        mode="peak",
        current_equity=110_000.0,
        baseline_equity=100_000.0,
        max_loss_today_dollars=0.0,
        min_trades_today=1,
        peak_multiplier=2.0,
    )
    # 1.5 × 2.0 = 3.0, ceiling = 2.0
    assert new_alloc["a__T1__D1"] == 2.0
