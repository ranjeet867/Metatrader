"""tests/test_risk_tiers.py — risk-tier mapping correctness."""
from __future__ import annotations

import pytest

from core.risk_tiers import (
    TierAssignment, apply_tier_multiplier, get_tier,
    list_satellite_cells, list_blacklist_cells,
)


def test_unknown_cell_defaults_to_core():
    t = get_tier(strategy="ema_pullback", ticker="EURUSD", tf="M15")
    assert t.tier == "CORE"
    assert t.multiplier == 1.0
    assert t.reason is None


def test_xpdusd_ema_spread_d1_is_satellite():
    t = get_tier(strategy="ema_spread_pullback",
                 ticker="XPDUSD", tf="D1")
    assert t.tier == "SATELLITE"
    assert t.multiplier == 0.5
    assert t.reason is not None
    assert "DD" in t.reason or "drawdown" in t.reason.lower()


def test_apply_tier_multiplier_for_satellite_halves_risk():
    sat = TierAssignment(tier="SATELLITE", multiplier=0.5, reason="x")
    assert apply_tier_multiplier(
        default_risk_pct=0.05, tier=sat) == pytest.approx(0.025)


def test_apply_tier_multiplier_for_core_is_identity():
    core = TierAssignment(tier="CORE", multiplier=1.0, reason=None)
    assert apply_tier_multiplier(
        default_risk_pct=0.05, tier=core) == 0.05


def test_apply_tier_multiplier_for_blacklist_zeroes_risk():
    bl = TierAssignment(tier="BLACKLIST", multiplier=0.0, reason="x")
    assert apply_tier_multiplier(
        default_risk_pct=0.05, tier=bl) == 0.0


def test_list_satellite_cells_includes_xpdusd():
    rows = list_satellite_cells()
    assert any(s == "ema_spread_pullback" and t == "XPDUSD" and tf == "D1"
               for (s, t, tf, _) in rows)


def test_list_blacklist_cells_returns_empty_initially():
    assert list_blacklist_cells() == []
