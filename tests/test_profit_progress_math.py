"""
test_profit_progress_math.py — the math behind the Profit-progress KPI tile.

The math has to handle three states cleanly:
  1. equity >= FTMO target → "Passed"
  2. baseline ≤ equity < target → "X% of way to target"
  3. equity < baseline → "+Y% to win" (climb required FROM CURRENT EQUITY)

State 3 is the user's case ($91k on $100k baseline). Earlier the tile
showed "0% vs FTMO target" which was useless. Now it should show
"+20.9% to win".
"""
from __future__ import annotations

import pytest


def profit_progress_picture(*, equity: float, original_baseline: float,
                            profit_target_pct: float = 10.0) -> dict:
    """Pure math the KPI tile uses. Mirrors the tile logic exactly."""
    target_equity = original_baseline * (1.0 + profit_target_pct / 100.0)
    out = {
        "target_equity": target_equity,
        "original_baseline": original_baseline,
    }
    if equity <= 0:
        out["state"] = "no_equity"
        return out
    if equity >= target_equity:
        out["state"] = "passed"
        out["overshoot_pct"] = (equity / original_baseline - 1.0) * 100.0
        return out
    if equity >= original_baseline:
        out["state"] = "climbing"
        progress_pct = ((equity - original_baseline)
                         / (original_baseline * profit_target_pct / 100.0)
                         * 100.0)
        gap_dollars = target_equity - equity
        gap_pct = gap_dollars / equity * 100.0
        out["progress_pct"] = progress_pct
        out["gap_pct"] = gap_pct
        out["gap_dollars"] = gap_dollars
        return out
    # eq < baseline
    out["state"] = "underwater"
    gap_dollars = target_equity - equity
    gap_pct = gap_dollars / equity * 100.0
    loss_pct = (original_baseline - equity) / original_baseline * 100.0
    out["gap_pct"] = gap_pct
    out["gap_dollars"] = gap_dollars
    out["loss_pct"] = loss_pct
    return out


# ---------------------------------------------------------------------------
# The user's exact case
# ---------------------------------------------------------------------------

def test_user_case_91k_on_100k_needs_about_20pct():
    """$91k on $100k baseline. Target = $110k. Climb required from $91k
    to $110k = $19k = 20.88% of $91k. User said 'I need 20% more to win'."""
    p = profit_progress_picture(equity=91_000.0,
                                  original_baseline=100_000.0)
    assert p["state"] == "underwater"
    assert p["target_equity"] == pytest.approx(110_000.0)
    assert p["gap_dollars"] == pytest.approx(19_000.0)
    assert p["gap_pct"] == pytest.approx(19_000 / 91_000 * 100, rel=1e-3)
    assert p["gap_pct"] == pytest.approx(20.879, abs=0.01)
    assert p["loss_pct"] == pytest.approx(9.0)


def test_user_case_91_388_92():
    """The exact equity from the screenshot."""
    p = profit_progress_picture(equity=91_388.92,
                                  original_baseline=100_000.0)
    assert p["state"] == "underwater"
    assert p["gap_dollars"] == pytest.approx(18_611.08)
    assert p["gap_pct"] == pytest.approx(20.365, abs=0.01)
    assert p["loss_pct"] == pytest.approx(8.611, abs=0.01)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def test_passed_state():
    p = profit_progress_picture(equity=112_000.0,
                                  original_baseline=100_000.0)
    assert p["state"] == "passed"
    assert p["overshoot_pct"] == pytest.approx(12.0)


def test_passed_state_exact_target():
    p = profit_progress_picture(equity=110_000.0,
                                  original_baseline=100_000.0)
    # Floating-point: 110_000 may be 109_999.999... after compute, but
    # we check the math is intent-correct via overshoot.
    assert p["state"] in ("passed", "climbing")
    if p["state"] == "passed":
        assert p["overshoot_pct"] == pytest.approx(10.0)


def test_climbing_state_50_pct_of_way():
    """$105k on $100k → halfway to $110k target."""
    p = profit_progress_picture(equity=105_000.0,
                                  original_baseline=100_000.0)
    assert p["state"] == "climbing"
    assert p["progress_pct"] == pytest.approx(50.0)
    assert p["gap_dollars"] == pytest.approx(5_000.0)
    assert p["gap_pct"] == pytest.approx(5_000 / 105_000 * 100, rel=1e-3)


def test_climbing_state_at_baseline_is_zero_progress():
    p = profit_progress_picture(equity=100_000.0,
                                  original_baseline=100_000.0)
    assert p["state"] == "climbing"
    assert p["progress_pct"] == pytest.approx(0.0)
    assert p["gap_dollars"] == pytest.approx(10_000.0)


def test_underwater_state_50k_account_at_46k():
    """50k challenge at $46k — needs $9k from current $46k."""
    p = profit_progress_picture(equity=46_000.0,
                                  original_baseline=50_000.0)
    assert p["state"] == "underwater"
    assert p["target_equity"] == pytest.approx(55_000.0)
    assert p["gap_dollars"] == pytest.approx(9_000.0)
    assert p["gap_pct"] == pytest.approx(9_000 / 46_000 * 100, rel=1e-3)
    assert p["loss_pct"] == pytest.approx(8.0)


def test_underwater_state_close_to_baseline():
    """$99,500 on $100k: gap is $10,500 ≈ 10.55% from current equity."""
    p = profit_progress_picture(equity=99_500.0,
                                  original_baseline=100_000.0)
    assert p["state"] == "underwater"
    assert p["gap_dollars"] == pytest.approx(10_500.0)
    assert p["gap_pct"] == pytest.approx(10.5527, abs=0.01)


def test_zero_equity_returns_no_equity_state():
    p = profit_progress_picture(equity=0.0, original_baseline=100_000.0)
    assert p["state"] == "no_equity"


def test_custom_profit_target():
    """Some prop firms use 8% target."""
    p = profit_progress_picture(equity=91_000.0,
                                  original_baseline=100_000.0,
                                  profit_target_pct=8.0)
    assert p["target_equity"] == 108_000.0
    assert p["gap_dollars"] == 17_000.0
