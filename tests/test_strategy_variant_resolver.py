"""
test_strategy_variant_resolver.py — pin the variant→base resolver used
by the Portfolio Composer's Quick-actions panel.

Bug history:
  Naive suffix-stripping mapped 'donchian_55' to 'donchian', but the
  registered base class is 'donchian_breakout'. Same problem for
  rsi_30_70 → rsi_meanrev and bbands_20_2 → bbands_meanrev. The
  resolver needs explicit aliases for these renames.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_composer_resolver():
    """Import the _resolve_base_strategy function from the composer
    page. The page filename has emoji + path module, so we import via
    importlib spec to get a clean handle to the function."""
    page_path = ROOT / "dashboards/pages/A_📦_Portfolio_Composer.py"
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("_pc", page_path)
    mod = importlib.util.module_from_spec(spec)
    # The page calls main() at import time. Patch streamlit so it
    # doesn't blow up during import.
    import streamlit as st                                                # noqa: E402
    real_set_page_config = st.set_page_config
    st.set_page_config = lambda *a, **kw: None
    try:
        spec.loader.exec_module(mod)
    finally:
        st.set_page_config = real_set_page_config
    return mod._resolve_base_strategy


# Real registry as of repo scan
REGISTERED = {
    "bbands_meanrev":    None,
    "donchian_breakout": None,
    "ema_cross":         None,
    "ema_pullback":      None,
    "first30_meanrev":   None,
    "ibs":               None,
    "inside_bar":        None,
    "orb":               None,
    "overnight_drift":   None,
    "rsi_meanrev":       None,
    "vol_breakout":      None,
}


@pytest.mark.parametrize("variant, expected", [
    # Direct hits — variant name == registered name
    ("ema_cross",         "ema_cross"),
    ("ema_pullback",      "ema_pullback"),
    ("vol_breakout",      "vol_breakout"),
    ("orb",               "orb"),
    # Param-suffix variants — strip suffix
    ("ema_cross_9_20",    "ema_cross"),
    ("ema_cross_12_26",   "ema_cross"),
    ("ema_pullback_20_50", "ema_pullback"),
    # Renamed-base aliases — must use the full registered name
    ("donchian_20",       "donchian_breakout"),
    ("donchian_55",       "donchian_breakout"),
    ("rsi_30_70",         "rsi_meanrev"),
    ("bbands_20_2",       "bbands_meanrev"),
    # Unknown variant → None
    ("does_not_exist",     None),
    ("foo_bar_baz",        None),
])
def test_variant_resolver_maps_correctly(variant, expected):
    resolve = _load_composer_resolver()
    assert resolve(variant, REGISTERED) == expected, (
        f"{variant!r} should map to {expected!r}"
    )


def test_donchian_55_does_not_strip_to_donchian():
    """Specifically pin the bug: donchian_55 must map to
    donchian_breakout, NOT 'donchian' (which doesn't exist)."""
    resolve = _load_composer_resolver()
    assert resolve("donchian_55", REGISTERED) == "donchian_breakout"
    assert "donchian" not in REGISTERED  # sanity: bare 'donchian' isn't a strategy


# ---------------------------------------------------------------------------
# Test the shared resolver directly (no streamlit page import needed).
# This is the new canonical home for the resolver — every page imports
# from here. The composer-level test above will keep working as a smoke
# check on the page-level shim.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(ROOT))
from dashboards.components.strategy_resolver import (   # noqa: E402
    resolve_base_strategy,
)


@pytest.mark.parametrize("variant, expected", [
    ("donchian_20",       "donchian_breakout"),
    ("donchian_55",       "donchian_breakout"),
    ("rsi_30_70",         "rsi_meanrev"),
    ("bbands_20_2",       "bbands_meanrev"),
    ("first30_meanrev",   "first30_meanrev"),
    ("ema_cross",         "ema_cross"),
    ("ema_cross_9_20",    "ema_cross"),
    ("trend_pullback",    "trend_pullback"),
    ("range_reversal",    "range_reversal"),
    ("not_a_strategy",     None),
])
def test_shared_resolver(variant, expected):
    """Direct test of the canonical resolver in
    dashboards/components/strategy_resolver.py — used by Strategy Library,
    Strategy Compare, Portfolio Composer, and deployment_dialogs."""
    registered = dict(REGISTERED)
    # add the new green-zone strategies to the simulated registry
    registered["trend_pullback"] = None
    registered["range_reversal"] = None
    assert resolve_base_strategy(variant, registered) == expected
