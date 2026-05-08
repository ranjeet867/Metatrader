"""
trendo_matrix.py — formalise the Risk:Reward × Win-Rate profitability
matrix that every trader knows.

The math:
    EV (in R per trade) = WR × R:R − (1 − WR)

Where WR is win rate (0–1) and R:R is avg_win / |avg_loss|. A trader is
profitable in the long run iff EV > 0.

Required win rate to be profitable for a given R:R:
    WR* = 1 / (1 + R:R)

So:
    1:1   needs WR > 50%      (break-even at 50%)
    1.5:1 needs WR > 40%
    2:1   needs WR > 33.3%
    3:1   needs WR > 25%
    4:1   needs WR > 20%
    5:1   needs WR > 16.7%

This module:
    • computes EV for any (WR, R:R) cell
    • classifies cells into green / amber / red zones
    • renders the static reference matrix (the Trendo chart)
    • flags whether a strategy lands in the profitable zone with a margin

Used by:
    • core.edge_catalog.EdgeStat (as a property)
    • Strategy Library + Portfolio Composer (filter + visual)
    • Tests pin every cell of the reference matrix
"""
from __future__ import annotations

from dataclasses import dataclass


# Margins around break-even — used for amber-zone classification
EV_GREEN_MIN = 0.05      # ≥ 5% EV per R = solidly profitable
EV_AMBER_MIN = -0.05     # within 5% of break-even = amber
# below EV_AMBER_MIN = red (definitively unprofitable)


def expectancy_per_R(win_rate_pct: float, rr_ratio: float) -> float:
    """Expected R per trade. Positive = profitable in the long run.

    A return of +0.20 means: on a 1% risk-per-trade base, the strategy
    averages +0.20% gain per trade (or +20% of a single 1R loss size).
    """
    if rr_ratio <= 0:
        return -1.0      # Pure loser if R:R is zero/negative
    wr = max(0.0, min(1.0, win_rate_pct / 100.0))
    return wr * rr_ratio - (1.0 - wr)


def required_win_rate_pct(rr_ratio: float) -> float:
    """Minimum win rate (in %) to break even at this R:R."""
    if rr_ratio <= 0:
        return 100.0
    return (1.0 / (1.0 + rr_ratio)) * 100.0


def trendo_zone(win_rate_pct: float, rr_ratio: float) -> str:
    """Classify a (WR, R:R) cell as 'green' | 'amber' | 'red'."""
    ev = expectancy_per_R(win_rate_pct, rr_ratio)
    if ev >= EV_GREEN_MIN:
        return "green"
    if ev >= EV_AMBER_MIN:
        return "amber"
    return "red"


def trendo_label(win_rate_pct: float, rr_ratio: float) -> str:
    """Compact label for tables: '✅ EV +0.42R' etc."""
    ev = expectancy_per_R(win_rate_pct, rr_ratio)
    icon = {"green": "✅", "amber": "🟡", "red": "🔴"}[
        trendo_zone(win_rate_pct, rr_ratio)
    ]
    return f"{icon} EV {ev:+.2f}R"


@dataclass(frozen=True)
class TrendoCell:
    win_rate_pct: float
    rr_ratio: float
    expectancy_R: float
    zone: str               # 'green' | 'amber' | 'red'
    is_profitable: bool


def classify(win_rate_pct: float, rr_ratio: float) -> TrendoCell:
    """Single entry point for downstream code. Wraps zone + EV + flag."""
    ev = expectancy_per_R(win_rate_pct, rr_ratio)
    z = trendo_zone(win_rate_pct, rr_ratio)
    return TrendoCell(
        win_rate_pct=win_rate_pct,
        rr_ratio=rr_ratio,
        expectancy_R=ev,
        zone=z,
        is_profitable=(ev > 0.0),
    )


# ---------------------------------------------------------------------------
# Reference matrix — pins the Trendo chart values exactly so the dashboard
# can render the same "every trader should know this" table on demand.
# ---------------------------------------------------------------------------

REF_WIN_RATES = (20, 30, 40, 50, 60)            # %
REF_RR_RATIOS = (1.0, 2.0, 3.0, 4.0, 5.0)


def reference_matrix() -> dict[tuple[float, float], str]:
    """Exact static reference grid: (rr_ratio, win_rate_pct) → zone label.

    Matches the classic chart at a 5% margin tolerance. Used by tests to
    confirm our generic classifier reproduces the textbook grid."""
    out: dict[tuple[float, float], str] = {}
    for rr in REF_RR_RATIOS:
        for wr in REF_WIN_RATES:
            ev = expectancy_per_R(wr, rr)
            if ev >= EV_GREEN_MIN:
                out[(rr, wr)] = "Profitable"
            elif ev >= EV_AMBER_MIN:
                out[(rr, wr)] = "Break Even"
            else:
                out[(rr, wr)] = "Not Profitable"
    return out
