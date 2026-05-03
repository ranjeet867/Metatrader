"""
test_position_sizer.py — INVARIANT-3 sizing path. Pure-function tests.

Spec acceptance cases:
  - US100.cash 70-pt stop: lots ≈ 3.9 at 0.3% on $91,400
  - USDJPY 50-pip stop: lots ≈ 0.83 at 0.3% on $91,400
  - XAUUSD $5 stop: lots ≈ 0.55 at 0.3% on $91,400
  - Volume step rounding: raw=3.91 → floored to 3.9 (step=0.1)
  - Reject: stop_distance=0
  - Reject: tick metadata missing
  - Reject: rounding-up would exceed 110% risk budget
"""
from __future__ import annotations

import pytest

from core.position_sizer import (
    SizingResult,
    SymbolInfo,
    calc_lots,
    floor_to_step,
)


# ---------------------------------------------------------------------------
# Symbol metadata fixtures (chosen so the math reproduces the spec exactly)
# ---------------------------------------------------------------------------

US100 = SymbolInfo(
    name="US100.cash",
    tick_size=0.01,        # broker quotes US100 to 2 decimals
    tick_value=0.01,       # $0.01 per tick per lot → 1 point = $1/lot
    volume_step=0.1,
    volume_min=0.1,
    volume_max=100.0,
    digits=2,
    contract_size=1.0,
)

# USDJPY: stop=0.50 (50 pips at 4-digit broker, or 500 ticks at 5-digit).
# Pick tick metadata that yields ≈$330/lot at SL so 274.20 / 330 ≈ 0.83.
USDJPY = SymbolInfo(
    name="USDJPY",
    tick_size=0.001,
    tick_value=0.661,      # ≈$0.661 per tick per lot at rate ~150
    volume_step=0.01,
    volume_min=0.01,
    volume_max=100.0,
    digits=3,
    contract_size=100_000,
)

# XAUUSD: stop=$5.00. money_per_lot ≈ $500 → 274.20/500 ≈ 0.55.
XAUUSD = SymbolInfo(
    name="XAUUSD",
    tick_size=0.01,
    tick_value=0.10,       # $0.10/tick/lot → $10/$1 movement → $50 per $5 stop... want $500
    volume_step=0.01,
    volume_min=0.01,
    volume_max=200.0,
    digits=2,
    contract_size=100,
)
# Recompute: stop=5, tick_size=0.01 → 500 ticks × 0.10 = $50/lot. Wrong.
# We want money_per_lot=$500 for the spec answer. Use tick_value=1.00.
XAUUSD = SymbolInfo(
    name="XAUUSD",
    tick_size=0.01, tick_value=1.0,    # $1/tick/lot → $5 stop = $500/lot
    volume_step=0.01, volume_min=0.01, volume_max=200.0,
    digits=2, contract_size=100,
)


EQUITY = 91_400.0
RISK_PCT = 0.3
RISK_DOLLARS = EQUITY * RISK_PCT / 100   # = $274.20


# ---------------------------------------------------------------------------
# Acceptance cases from the spec
# ---------------------------------------------------------------------------

class TestSpecAcceptanceCases:
    def test_us100_70pt_stop_3_9_lots(self):
        """70-pt stop on US100.cash at 0.3% risk on $91,400 → 3.9 lots."""
        r = calc_lots(equity=EQUITY, risk_pct=RISK_PCT,
                       entry_price=18_000.00, stop_price=17_930.00,
                       sym=US100)
        assert r.ok, f"unexpected reject: {r.reason}"
        assert r.lots == pytest.approx(3.9)
        # 3.9 lots × $70/lot = $273
        assert r.money_risk == pytest.approx(273.0)
        assert r.intended_risk == pytest.approx(274.20)

    def test_usdjpy_50pip_stop_lots(self):
        """50-pip USDJPY stop at 0.3% risk → ~0.83 lots."""
        r = calc_lots(equity=EQUITY, risk_pct=RISK_PCT,
                       entry_price=150.00, stop_price=149.50,
                       sym=USDJPY)
        assert r.ok, f"unexpected reject: {r.reason}"
        assert 0.78 < r.lots < 0.88   # spec says ≈ 0.83; allow tolerance
        # money_per_lot = 500 ticks × 0.661 = $330.50
        # raw = 274.20 / 330.50 = 0.8296 → floor to 0.82 (step 0.01)
        assert r.lots == pytest.approx(0.82, abs=0.01)

    def test_xauusd_5dollar_stop_lots(self):
        """$5 stop on gold at 0.3% risk → ~0.55 lots."""
        r = calc_lots(equity=EQUITY, risk_pct=RISK_PCT,
                       entry_price=2_000.00, stop_price=1_995.00,
                       sym=XAUUSD)
        assert r.ok
        # money_per_lot = (5/0.01) × 1.0 = $500/lot → 274.20/500 = 0.5484
        # floor to 0.01 → 0.54 (step 0.01).
        assert r.lots == pytest.approx(0.54, abs=0.01)


# ---------------------------------------------------------------------------
# Volume-step rounding
# ---------------------------------------------------------------------------

class TestVolumeStepRounding:
    def test_raw_3_91_floors_to_3_9(self):
        """raw=3.91 lots, step=0.1 → 3.9."""
        # Engineer inputs to give exactly raw_lots = 3.91
        # money_per_lot = 100, risk_amount = 391 → 391/100 = 3.91
        sym = SymbolInfo("X", tick_size=1.0, tick_value=1.0,
                          volume_step=0.1, volume_min=0.1,
                          volume_max=100, digits=0, contract_size=1)
        r = calc_lots(equity=39_100, risk_pct=1.0,
                       entry_price=200, stop_price=100, sym=sym)
        # money_per_lot = (100/1)*1 = 100; risk = 391; raw = 3.91
        assert r.ok
        assert r.lots == pytest.approx(3.9)

    def test_floor_to_step_helper(self):
        """floor_to_step is the underlying helper."""
        assert floor_to_step(3.91, 0.1) == pytest.approx(3.9)
        assert floor_to_step(3.99, 0.1) == pytest.approx(3.9)
        assert floor_to_step(0.012, 0.01) == pytest.approx(0.01)
        # Exact boundary
        assert floor_to_step(3.9, 0.1) == pytest.approx(3.9)


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------

class TestRejections:
    def test_stop_equals_entry_rejected(self):
        r = calc_lots(equity=100_000, risk_pct=1.0,
                       entry_price=100, stop_price=100, sym=US100)
        assert not r.ok
        assert r.reason == "stop_equals_entry"

    def test_tick_metadata_missing_rejected(self):
        bad = SymbolInfo("X", tick_size=0.0, tick_value=1.0,
                          volume_step=0.1, volume_min=0.1, volume_max=10,
                          digits=2, contract_size=1)
        r = calc_lots(equity=100_000, risk_pct=1.0,
                       entry_price=100, stop_price=99, sym=bad)
        assert not r.ok
        assert r.reason == "tick_metadata_missing"

    def test_invalid_inputs_zero_equity(self):
        r = calc_lots(equity=0, risk_pct=1.0,
                       entry_price=100, stop_price=99, sym=US100)
        assert not r.ok
        assert r.reason == "invalid_inputs"

    def test_invalid_inputs_zero_risk_pct(self):
        r = calc_lots(equity=100_000, risk_pct=0,
                       entry_price=100, stop_price=99, sym=US100)
        assert not r.ok
        assert r.reason == "invalid_inputs"

    def test_below_min_lot_rejected_when_overshoot_too_big(self):
        """Tiny equity / huge stop → raw < volume_min, and pumping to
        volume_min would risk way over budget."""
        # raw = 0.001 lots, volume_min=0.1 → bump would risk 100x budget
        sym = SymbolInfo("X", tick_size=1.0, tick_value=1.0,
                          volume_step=0.1, volume_min=0.1,
                          volume_max=10, digits=0, contract_size=1)
        # money_per_lot = 100, risk_amount = 0.1 → raw = 0.001
        # Bumping to 0.1 lots would risk $10 vs $0.10 budget = 100x over
        r = calc_lots(equity=10, risk_pct=1.0,
                       entry_price=200, stop_price=100, sym=sym)
        assert not r.ok
        assert r.reason == "below_min_lot_would_exceed_risk"

    def test_below_min_within_overshoot_tolerance_promoted(self):
        """raw=0.095, volume_min=0.1 → bump from 0.095 to 0.1 is only ~5%
        overshoot; allow."""
        sym = SymbolInfo("X", tick_size=1.0, tick_value=1.0,
                          volume_step=0.1, volume_min=0.1,
                          volume_max=10, digits=0, contract_size=1)
        # money_per_lot = 100, risk_amount = 9.5 → raw = 0.095
        # min lots = 0.1 → risk = 10 (5.3% over) → allowed
        r = calc_lots(equity=950, risk_pct=1.0,
                       entry_price=200, stop_price=100, sym=sym)
        assert r.ok
        assert r.lots == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Volume max clamp
# ---------------------------------------------------------------------------

class TestVolumeMaxClamp:
    def test_volume_max_clamps_lots_down(self):
        sym = SymbolInfo("X", tick_size=1.0, tick_value=1.0,
                          volume_step=0.1, volume_min=0.1,
                          volume_max=2.0, digits=0, contract_size=1)
        # money_per_lot = 100, risk_amount = 1000 → raw = 10 lots
        # but volume_max = 2.0 → clamp to 2.0
        r = calc_lots(equity=100_000, risk_pct=1.0,
                       entry_price=200, stop_price=100, sym=sym)
        assert r.ok
        assert r.lots == pytest.approx(2.0)
        # money_risk reflects the clamped (smaller) lots, not the budget
        assert r.money_risk == pytest.approx(200.0)
        assert r.intended_risk == pytest.approx(1000.0)


class TestSizingResultBoolean:
    def test_truthy_when_ok(self):
        sym = US100
        r = calc_lots(equity=EQUITY, risk_pct=RISK_PCT,
                       entry_price=18_000, stop_price=17_930, sym=sym)
        assert r           # __bool__ True
        if not r:
            pytest.fail("SizingResult should be truthy when ok=True")

    def test_falsy_when_rejected(self):
        r = calc_lots(equity=0, risk_pct=1.0,
                       entry_price=100, stop_price=99, sym=US100)
        assert not r
