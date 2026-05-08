"""
risk_tiers.py — per-cell risk-tier classification (CORE / SATELLITE).

Most catalog cells deploy at the default risk_pct (e.g. 0.05% per
trade). A few cells have edge but also a high drawdown floor — they
deserve a place in the book but at HALF the size so they can't
dominate the FTMO daily-loss limit.

Example: XPDUSD ema_spread_pullback has PF 2.53 and mean_R +1.12 —
genuinely strong — but max DD 18.2%. At 0.05% risk it could chew
through the FTMO 10% total-loss buffer before recovering. Mark it
SATELLITE → 0.025% risk → DD floor halves to ~9% which fits.

Usage from the Composer or runner:
    from core.risk_tiers import get_tier, apply_tier_multiplier
    tier = get_tier(strategy=..., ticker=..., tf=...)
    sized_risk = apply_tier_multiplier(default_risk_pct=0.05, tier=tier)

The mapping is data, not code — edit SATELLITE_CELLS and BLACKLIST
below to change the policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Tier = Literal["CORE", "SATELLITE", "BLACKLIST"]


# Cells flagged as SATELLITE — half-risk because they have edge but
# elevated drawdown. Tuple key: (strategy, ticker, tf).
SATELLITE_CELLS: dict[tuple[str, str, str], str] = {
    ("ema_spread_pullback", "XPDUSD", "D1"):
        "PF 2.53, mean_R +1.12, but 18.2% DD — half-size to stay "
        "inside FTMO 10% floor when DD hits.",
    # Future satellites get added here as the catalog evolves.
    # Example pattern:
    # ("orb", "JP225.cash", "M15"): "edge present but 12% DD",
}


# Cells flagged as BLACKLIST — never auto-deploy. Use for cells that
# look good in metrics but have known structural issues (data gaps,
# regime collapse, fail audit).
BLACKLIST_CELLS: dict[tuple[str, str, str], str] = {
    # Empty for now. Populate from edge-decay autolearner / forensic
    # snapshot post-mortems.
}


# Risk multipliers per tier. CORE = 1.0 (full risk), SATELLITE = 0.5
# (half risk). BLACKLIST = 0.0 (effectively disabled).
TIER_MULTIPLIERS: dict[Tier, float] = {
    "CORE": 1.0,
    "SATELLITE": 0.5,
    "BLACKLIST": 0.0,
}


@dataclass(frozen=True)
class TierAssignment:
    tier: Tier
    multiplier: float
    reason: str | None    # human-readable rationale, or None for CORE


def get_tier(*, strategy: str, ticker: str, tf: str) -> TierAssignment:
    """Look up a cell's risk tier. Defaults to CORE if not flagged."""
    key = (strategy, ticker, tf)
    if key in BLACKLIST_CELLS:
        return TierAssignment(
            tier="BLACKLIST", multiplier=TIER_MULTIPLIERS["BLACKLIST"],
            reason=BLACKLIST_CELLS[key],
        )
    if key in SATELLITE_CELLS:
        return TierAssignment(
            tier="SATELLITE", multiplier=TIER_MULTIPLIERS["SATELLITE"],
            reason=SATELLITE_CELLS[key],
        )
    return TierAssignment(tier="CORE", multiplier=1.0, reason=None)


def apply_tier_multiplier(
    *, default_risk_pct: float, tier: TierAssignment
) -> float:
    """Return adjusted risk_pct after applying the tier multiplier.

    >>> apply_tier_multiplier(default_risk_pct=0.05,
    ...     tier=TierAssignment("SATELLITE", 0.5, ""))
    0.025
    """
    return float(default_risk_pct) * tier.multiplier


def list_satellite_cells() -> list[tuple[str, str, str, str]]:
    """Return [(strategy, ticker, tf, reason), …] for the dashboard."""
    return [(s, t, tf, reason)
            for (s, t, tf), reason in SATELLITE_CELLS.items()]


def list_blacklist_cells() -> list[tuple[str, str, str, str]]:
    return [(s, t, tf, reason)
            for (s, t, tf), reason in BLACKLIST_CELLS.items()]
