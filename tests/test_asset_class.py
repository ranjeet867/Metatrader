"""
test_asset_class.py — symbol classification.
"""
from __future__ import annotations

import pytest

from core.asset_class import classify


# Default overrides match those in core.config.DEFAULT_CONFIG
DEFAULT_OVERRIDES = {
    "stock":  ["META", "NVDA", "AAPL", "TSLA", "MSFT", "AMZN", "GOOGL"],
    "index":  ["US100.cash", "US500.cash", "GER40.cash", "EU50.cash",
                "US30", "NAS100", "SPX500", "JP225"],
    "metal":  ["XAUUSD", "XAGUSD", "XPTUSD"],
    "energy": ["USOIL", "UKOIL", "NGAS"],
    "fx":     [],
}


class TestHeuristic:
    """Without overrides — heuristic-only classification."""

    @pytest.mark.parametrize("sym", ["EURUSD", "GBPUSD", "USDJPY", "GBPJPY",
                                       "AUDUSD", "NZDUSD", "EURGBP"])
    def test_six_letter_alpha_is_fx(self, sym):
        assert classify(sym) == "fx"

    @pytest.mark.parametrize("sym", ["XAUUSD", "XAGUSD", "XPTUSD"])
    def test_metal_prefix_is_metal(self, sym):
        assert classify(sym) == "metal"

    @pytest.mark.parametrize("sym", ["USOIL", "UKOIL", "NGAS", "BRENT", "WTI"])
    def test_energy_tokens_is_energy(self, sym):
        assert classify(sym) == "energy"

    @pytest.mark.parametrize("sym", ["US100.cash", "US500.cash", "GER40.cash",
                                       "EU50.cash", "JP225", "NAS100", "SPX500",
                                       "US30"])
    def test_index_number_is_index(self, sym):
        assert classify(sym) == "index"

    def test_unknown_falls_to_other(self):
        # Stock tickers (1-5 letters) don't match the 6-letter FX heuristic
        # — without an override they fall to 'other'. This is the EXPECTED
        # behaviour: stocks must be declared explicitly via overrides.
        assert classify("META") == "other"
        assert classify("NVDA") == "other"
        assert classify("ABC") == "other"

    def test_empty_symbol_is_other(self):
        assert classify("") == "other"
        assert classify("   ") == "other"


class TestOverrides:
    """Overrides take precedence over heuristic — required for stocks."""

    def test_stock_via_override(self):
        assert classify("META", overrides=DEFAULT_OVERRIDES) == "stock"
        assert classify("NVDA", overrides=DEFAULT_OVERRIDES) == "stock"

    def test_override_strip_suffix_match(self):
        """Broker suffixes (.cash) should match override entries either way."""
        # US100 (no suffix) matches "US100.cash" via stripped comparison
        assert classify("US100", overrides=DEFAULT_OVERRIDES) == "index"
        # And US100.cash matches itself directly
        assert classify("US100.cash", overrides=DEFAULT_OVERRIDES) == "index"

    def test_override_case_insensitive(self):
        assert classify("meta", overrides=DEFAULT_OVERRIDES) == "stock"
        assert classify("Meta", overrides=DEFAULT_OVERRIDES) == "stock"

    def test_overrides_can_reclassify_heuristic(self):
        """If user overrides EURUSD as 'metal' (ridiculous, but allowed),
        the override wins."""
        weird = {"metal": ["EURUSD"]}
        assert classify("EURUSD", overrides=weird) == "metal"

    def test_no_override_falls_back_to_heuristic(self):
        empty = {"stock": [], "index": [], "metal": [], "energy": [], "fx": []}
        assert classify("XAUUSD", overrides=empty) == "metal"
        assert classify("EURUSD", overrides=empty) == "fx"


class TestRealWorldSymbols:
    """Smoke against the symbol set actually used in this repo."""

    def test_repo_symbols_classify_correctly(self):
        cases = {
            "US100.cash": "index",
            "US500.cash": "index",
            "GER40.cash": "index",
            "EU50.cash":  "index",
            "EURUSD":     "fx",
            "GBPUSD":     "fx",
            "AUDUSD":     "fx",
            "NZDUSD":     "fx",
            "USDJPY":     "fx",
            "GBPJPY":     "fx",
        }
        for sym, want in cases.items():
            got = classify(sym, overrides=DEFAULT_OVERRIDES)
            assert got == want, f"classify({sym!r}) → {got!r}, expected {want!r}"
