"""
test_mt5_account_phase2.py — bridge methods added in Phase 2:
  positions_get / position_close / history_deals_get
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.mt5_account import (
    BridgeError,
    MT5AccountClient,
)


def _mock(responses: dict):
    calls = []
    def call(method, params):
        calls.append((method, params))
        if method not in responses:
            raise KeyError(f"unmocked method {method!r}")
        return responses[method]
    call.calls = calls
    return call


class TestPositionsGet:
    def test_parses_minimal_shape(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "positions_get": [
                {"ticket": 1, "symbol": "US100.cash", "type": 0,
                 "volume": 0.3, "price_open": 21000.0,
                 "price_current": 21010.0, "profit": 30.0,
                 "magic": 0, "comment": ""},
            ],
        }))
        out = bridge.positions_get()
        assert len(out) == 1
        assert out[0].ticket == 1
        assert out[0].symbol == "US100.cash"
        assert out[0].volume == 0.3
        assert out[0].profit == 30.0

    def test_parses_wrapped_data_shape(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "positions_get": {"ok": True, "data": [
                {"ticket": 5, "symbol": "EURUSD", "type": 1,
                 "volume": 1.0, "price_open": 1.10, "price_current": 1.10,
                 "profit": 0.0, "magic": 0, "comment": ""},
            ]},
        }))
        out = bridge.positions_get()
        assert len(out) == 1 and out[0].ticket == 5

    def test_empty_list_is_fine(self):
        bridge = MT5AccountClient(bridge_call=_mock({"positions_get": []}))
        assert bridge.positions_get() == []

    def test_malformed_row_raises(self):
        # Missing 'symbol' field
        bridge = MT5AccountClient(bridge_call=_mock({
            "positions_get": [{"ticket": 1, "type": 0, "volume": 1.0,
                                "price_open": 100.0}],
        }))
        with pytest.raises(BridgeError, match="malformed"):
            bridge.positions_get()

    def test_error_response_raises(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "positions_get": {"ok": False, "error": "no connection"},
        }))
        with pytest.raises(BridgeError, match="no connection"):
            bridge.positions_get()


class TestPositionClose:
    def test_happy_path(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "position_close": {"ok": True, "retcode": 10009,
                                "deal": 999, "price": 21001.0,
                                "comment": "ok"},
        }))
        res = bridge.position_close(ticket=1)
        assert res.ok and res.retcode == 10009
        assert res.deal == 999

    def test_error_raises(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "position_close": {"ok": False, "error": "ticket not found"},
        }))
        with pytest.raises(BridgeError, match="ticket not found"):
            bridge.position_close(ticket=99999)


class TestHistoryDealsGet:
    def test_parses_deal(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "history_deals_get": [
                {"ticket": 1, "order": 1, "position_id": 5,
                 "time_utc": "2026-05-05T13:00:00+00:00",
                 "type": 0, "entry": 1, "symbol": "US100.cash",
                 "volume": 0.3, "price": 21010.0,
                 "profit": 30.0, "swap": 0.0, "commission": -0.5,
                 "comment": ""},
            ],
        }))
        out = bridge.history_deals_get(
            since_utc=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert len(out) == 1
        assert out[0].position_id == 5
        assert out[0].profit == 30.0
        assert out[0].entry == 1

    def test_passes_since_until_through(self):
        calls = _mock({"history_deals_get": []})
        bridge = MT5AccountClient(bridge_call=calls)
        s = datetime(2026, 5, 1, tzinfo=timezone.utc)
        u = datetime(2026, 5, 5, tzinfo=timezone.utc)
        bridge.history_deals_get(since_utc=s, until_utc=u)
        assert calls.calls[0][0] == "history_deals_get"
        params = calls.calls[0][1]
        assert "since_utc" in params and "until_utc" in params

    def test_unknown_shape_raises(self):
        bridge = MT5AccountClient(bridge_call=_mock({
            "history_deals_get": "this is wrong",
        }))
        with pytest.raises(BridgeError):
            bridge.history_deals_get(
                since_utc=datetime(2026, 5, 1, tzinfo=timezone.utc),
            )
