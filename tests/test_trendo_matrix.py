"""
test_trendo_matrix.py — pin the Trendo R:R × Win-Rate profitability
matrix. The classic chart from the user's screenshot has 25 cells,
each labelled Profitable / Break Even / Not Profitable. Our classifier
must reproduce them exactly.
"""
from __future__ import annotations

import pytest

from core import trendo_matrix as tm


# ---------------------------------------------------------------------------
# Reference matrix — exact textbook grid
# ---------------------------------------------------------------------------

# (R:R, win_rate%, expected_label)
EXPECTED = [
    # 1:1
    (1.0, 20, "Not Profitable"),
    (1.0, 30, "Not Profitable"),
    (1.0, 40, "Not Profitable"),
    (1.0, 50, "Break Even"),
    (1.0, 60, "Profitable"),
    # 2:1
    (2.0, 20, "Not Profitable"),
    (2.0, 30, "Not Profitable"),    # WR* = 33.3, 30 < 33.3 → unprofitable
    (2.0, 40, "Profitable"),
    (2.0, 50, "Profitable"),
    (2.0, 60, "Profitable"),
    # 3:1
    (3.0, 20, "Not Profitable"),    # WR* = 25
    (3.0, 30, "Profitable"),
    (3.0, 40, "Profitable"),
    (3.0, 50, "Profitable"),
    (3.0, 60, "Profitable"),
    # 4:1
    (4.0, 20, "Break Even"),         # WR* = 20 — exactly at threshold
    (4.0, 30, "Profitable"),
    (4.0, 40, "Profitable"),
    (4.0, 50, "Profitable"),
    (4.0, 60, "Profitable"),
    # 5:1
    (5.0, 20, "Profitable"),         # WR* = 16.7 — well above threshold
    (5.0, 30, "Profitable"),
    (5.0, 40, "Profitable"),
    (5.0, 50, "Profitable"),
    (5.0, 60, "Profitable"),
]


@pytest.mark.parametrize("rr,wr,expected", EXPECTED)
def test_reference_matrix_matches_textbook(rr, wr, expected):
    grid = tm.reference_matrix()
    assert grid[(rr, wr)] == expected, (
        f"R:R={rr}, WR={wr}% → got '{grid[(rr,wr)]}', expected '{expected}'"
    )


# ---------------------------------------------------------------------------
# Math invariants
# ---------------------------------------------------------------------------

def test_expectancy_zero_at_break_even_point():
    """For 1:1, 50% WR is exactly EV = 0."""
    assert tm.expectancy_per_R(50.0, 1.0) == pytest.approx(0.0)


def test_expectancy_formula():
    # 60% WR @ 2:1 → 0.6 * 2 - 0.4 = 1.2 - 0.4 = 0.8
    assert tm.expectancy_per_R(60.0, 2.0) == pytest.approx(0.8)


def test_required_win_rate_inverse_of_rr():
    assert tm.required_win_rate_pct(1.0) == pytest.approx(50.0)
    assert tm.required_win_rate_pct(2.0) == pytest.approx(33.333, rel=1e-3)
    assert tm.required_win_rate_pct(3.0) == pytest.approx(25.0)
    assert tm.required_win_rate_pct(4.0) == pytest.approx(20.0)
    assert tm.required_win_rate_pct(5.0) == pytest.approx(16.667, rel=1e-3)


def test_zero_or_negative_rr_is_red():
    assert tm.trendo_zone(50.0, 0.0) == "red"
    assert tm.trendo_zone(50.0, -1.0) == "red"


def test_zone_labels_consistent_with_classify():
    cell = tm.classify(60.0, 2.0)
    assert cell.zone == "green"
    assert cell.is_profitable is True
    assert cell.expectancy_R > 0.05


def test_classify_handles_break_even():
    cell = tm.classify(50.0, 1.0)
    assert cell.zone == "amber"
    assert abs(cell.expectancy_R) < 0.05


def test_classify_obvious_loser():
    cell = tm.classify(20.0, 1.0)
    assert cell.zone == "red"
    assert cell.is_profitable is False


# ---------------------------------------------------------------------------
# Trendo label formatting
# ---------------------------------------------------------------------------

def test_label_includes_icon_and_ev():
    label = tm.trendo_label(60.0, 2.0)
    assert "✅" in label
    assert "EV" in label
    assert "+0.80R" in label

    label_loss = tm.trendo_label(20.0, 1.0)
    assert "🔴" in label_loss
    assert "-0.60R" in label_loss


def test_label_break_even_uses_amber_icon():
    label = tm.trendo_label(50.0, 1.0)
    assert "🟡" in label
