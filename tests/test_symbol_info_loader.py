"""
test_symbol_info_loader.py — JSON loader for SymbolInfo cache.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.position_sizer import SymbolInfo
from core.symbol_info_loader import load, load_all, try_load


def _tmp_path() -> Path:
    return Path(tempfile.mkstemp(suffix=".json")[1])


def _good() -> dict:
    return {
        "_meta": {"schema_version": 1},
        "symbols": {
            "US100.cash": {
                "tick_size": 0.01, "tick_value": 0.01,
                "volume_step": 0.1, "volume_min": 0.1, "volume_max": 100.0,
                "digits": 2, "contract_size": 1.0,
            },
            "EURUSD": {
                "tick_size": 0.00001, "tick_value": 1.0,
                "volume_step": 0.01, "volume_min": 0.01, "volume_max": 100.0,
                "digits": 5, "contract_size": 100000.0,
            },
        },
    }


def test_load_all_returns_dict_of_symbolinfo():
    p = _tmp_path()
    p.write_text(json.dumps(_good()))
    syms = load_all(p)
    assert "US100.cash" in syms
    assert "EURUSD" in syms
    assert isinstance(syms["US100.cash"], SymbolInfo)
    assert syms["US100.cash"].tick_size == 0.01


def test_load_single_symbol():
    p = _tmp_path()
    p.write_text(json.dumps(_good()))
    si = load("US100.cash", p)
    assert si.name == "US100.cash"
    assert si.tick_value == 0.01


def test_load_missing_file_raises():
    p = Path("/nonexistent/symbol_info.json")
    with pytest.raises(FileNotFoundError):
        load_all(p)


def test_load_invalid_json_raises():
    p = _tmp_path()
    p.write_text("{not valid json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_all(p)


def test_load_missing_field_raises():
    p = _tmp_path()
    bad = _good()
    del bad["symbols"]["US100.cash"]["tick_size"]
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="missing fields"):
        load_all(p)


def test_load_negative_tick_size_raises():
    p = _tmp_path()
    bad = _good()
    bad["symbols"]["US100.cash"]["tick_size"] = -0.01
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="tick_size"):
        load_all(p)


def test_try_load_returns_none_on_missing():
    p = Path("/nonexistent.json")
    assert try_load("US100.cash", p) is None


def test_try_load_returns_none_on_unknown_symbol():
    p = _tmp_path()
    p.write_text(json.dumps(_good()))
    assert try_load("BOGUS", p) is None


def test_load_unknown_symbol_raises():
    p = _tmp_path()
    p.write_text(json.dumps(_good()))
    with pytest.raises(KeyError):
        load("BOGUS", p)


def test_real_repo_symbol_info_loads_cleanly():
    """Sanity-check the checked-in data/symbol_info.json validates."""
    syms = load_all()
    assert "US100.cash" in syms
    # All survivor tickers must be present
    for s in ("US100.cash", "GER40.cash", "USDJPY"):
        assert s in syms, f"missing {s} in repo symbol_info.json"
