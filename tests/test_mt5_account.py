"""
test_mt5_account.py — bridge-backed account/symbol metadata, mocked.
"""
from __future__ import annotations

import time

import pytest

from core.mt5_account import MT5AccountClient


def _mock_bridge(responses: dict) -> callable:
    """Return a function that pops responses keyed by method."""
    calls = []

    def call(method: str, params: dict) -> dict:
        calls.append((method, params))
        if method not in responses:
            raise KeyError(f"unexpected method {method!r}")
        return responses[method]
    call.calls = calls
    return call


def test_account_info_parses_flat_shape():
    bridge = _mock_bridge({
        "account_info": {"login": 12345, "balance": 100_000.0,
                          "equity": 99_500.0, "currency": "USD",
                          "leverage": 30}
    })
    c = MT5AccountClient(bridge_call=bridge)
    ai = c.account_info()
    assert ai.login == 12345
    assert ai.balance == 100_000
    assert ai.currency == "USD"


def test_account_info_caches_within_ttl():
    bridge = _mock_bridge({
        "account_info": {"login": 1, "balance": 100, "equity": 100,
                          "currency": "USD", "leverage": 1}
    })
    c = MT5AccountClient(bridge_call=bridge, ttl_seconds=10.0)
    c.account_info()
    c.account_info()
    c.account_info()
    assert len(bridge.calls) == 1   # cached


def test_account_info_force_refresh_skips_cache():
    bridge = _mock_bridge({
        "account_info": {"login": 1, "balance": 100, "equity": 100,
                          "currency": "USD", "leverage": 1}
    })
    c = MT5AccountClient(bridge_call=bridge, ttl_seconds=60.0)
    c.account_info()
    c.account_info(force_refresh=True)
    assert len(bridge.calls) == 2


def test_symbol_info_parses_nested_shape():
    bridge = _mock_bridge({
        "symbol_info": {"data": {
            "tick_size": 0.01, "tick_value": 1.0,
            "volume_step": 0.1, "volume_min": 0.1, "contract_size": 1.0,
        }}
    })
    c = MT5AccountClient(bridge_call=bridge)
    si = c.symbol_info("US100.cash")
    assert si.tick_size == 0.01
    assert si.volume_step == 0.1


def test_symbol_info_error_response_raises():
    bridge = _mock_bridge({
        "symbol_info": {"ok": False, "error": "unknown symbol"}
    })
    c = MT5AccountClient(bridge_call=bridge)
    with pytest.raises(RuntimeError, match="symbol_info"):
        c.symbol_info("BOGUS")
