"""
test_library_legacy_decoder.py — verify _decode_legacy_variant_config()
reconstructs the right strategy params from a markdown-source catalog
row (variant name + R:R label).

Markdown rows in docs/optimization_*.md don't carry source_config_json,
so the Library deep-dive needs to infer params from the variant name
+ R:R label to reproduce the catalog metrics. This decoder is the
fallback when source_config_json is empty.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


# Load the Library page module by path because the filename has emoji
ROOT = Path(__file__).resolve().parents[1]
LIB_PATH = ROOT / "dashboards" / "pages" / "7_🏛️_Strategy_Library.py"


def _load_decoder():
    spec = importlib.util.spec_from_file_location(
        "lib_page_for_test", LIB_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Avoid Streamlit page_config side-effects by importing the function
    # via exec rather than full module load (which calls main()).
    src = LIB_PATH.read_text()
    # Extract just the _decode_legacy_variant_config function source.
    # We compile it in isolation so the rest of the page (which needs
    # Streamlit) doesn't load.
    start_marker = "def _decode_legacy_variant_config("
    end_marker = "def _run_drilldown("
    assert start_marker in src
    assert end_marker in src
    func_src = src[src.index(start_marker):src.index(end_marker)]
    namespace: dict = {}
    exec(func_src, namespace)
    return namespace["_decode_legacy_variant_config"]


@pytest.fixture(scope="module")
def decode():
    return _load_decoder()


def test_ema_cross_variant_decoded(decode):
    cfg = decode(variant="ema_cross_12_26", rr_label="1:2")
    assert cfg["fast_period"] == 12
    assert cfg["slow_period"] == 26
    assert cfg["stop_atr_mult"] == 1.5
    assert cfg["target_atr_mult"] == 3.0


def test_ema_cross_with_wide_rr(decode):
    """1:2 wide → stop=2.5, target=5.0 (mirrors core.optimizer.RR_VARIANTS)."""
    cfg = decode(variant="ema_cross_9_20", rr_label="1:2 wide")
    assert cfg["fast_period"] == 9
    assert cfg["slow_period"] == 20
    assert cfg["stop_atr_mult"] == 2.5
    assert cfg["target_atr_mult"] == 5.0


def test_donchian_period_decoded(decode):
    cfg = decode(variant="donchian_55", rr_label="1:1.5")
    assert cfg["period"] == 55
    assert cfg["stop_atr_mult"] == 1.5
    assert cfg["target_atr_mult"] == 2.25


def test_rsi_thresholds_decoded(decode):
    cfg = decode(variant="rsi_30_70", rr_label="1:1")
    assert cfg["oversold"] == 30
    assert cfg["overbought"] == 70
    assert cfg["stop_atr_mult"] == 1.5
    assert cfg["target_atr_mult"] == 1.5


def test_bbands_decoded(decode):
    cfg = decode(variant="bbands_20_2", rr_label="default")
    assert cfg["bb_period"] == 20
    assert cfg["bb_k"] == 2.0
    # 'default' isn't in the rr_table → no atr_mults set
    assert "stop_atr_mult" not in cfg


def test_ema_pullback_decoded(decode):
    cfg = decode(variant="ema_pullback_20_50", rr_label="default")
    assert cfg["fast_period"] == 20
    assert cfg["slow_period"] == 50


def test_unknown_variant_returns_partial_or_empty(decode):
    cfg = decode(variant="totally_unknown_xyz", rr_label="1:2")
    # Variant unrecognised, but R:R label still decodes.
    assert "fast_period" not in cfg
    assert "period" not in cfg
    assert cfg["stop_atr_mult"] == 1.5
    assert cfg["target_atr_mult"] == 3.0


def test_no_rr_label_no_atr_mults(decode):
    cfg = decode(variant="ema_cross_12_26", rr_label="")
    assert cfg["fast_period"] == 12
    assert "stop_atr_mult" not in cfg
