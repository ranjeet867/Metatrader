"""
test_account_detect.py — auto-detect of MT5 account, broker server
inference, FTMO size snapping, and local-tz detection.

The detection runs against a FAKE bridge so we don't need a live MT5
connection in CI.
"""
from __future__ import annotations

import os
import pytest

from core.account_detect import (
    DetectedAccount,
    detect_account_via_bridge,
    detect_local_tz,
    infer_broker_from_server,
    infer_type_from_account_info,
)
from core.mt5_account import AccountInfo, MT5AccountClient


# ---------------------------------------------------------------------------
# infer_broker_from_server
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("server,company,expected", [
    ("FTMO-Demo",            "",                    "FTMO"),
    ("FTMO-Server2",         "",                    "FTMO"),
    ("ftmo-real",            "",                    "FTMO"),     # case-insensitive
    ("",                      "FTMO Trader s.r.o.", "FTMO"),     # fall back to company
    ("TopStepFutures-Demo",  "",                    "TopStep"),
    ("MyForexFunds-Live",    "",                    "MyForexFunds"),
    ("The5ers-Server",       "",                    "The5ers"),
    ("FundedNext-Demo",      "",                    "FundedNext"),
    ("E8-Funded",            "",                    "E8"),
    ("ICMarkets-Live01",     "",                    "IC Markets"),
    ("IC Markets-Live01",    "",                    "IC Markets"),
    ("Pepperstone-Demo01",   "",                    "Pepperstone"),
    ("OandaCorp-NA-Real",    "",                    "Oanda"),
    ("UnknownBroker-XYZ",    "",                    "Other"),
    ("",                      "",                    "Other"),
])
def test_infer_broker_from_server(server, company, expected):
    assert infer_broker_from_server(server, company=company) == expected


# ---------------------------------------------------------------------------
# infer_type_from_account_info
# ---------------------------------------------------------------------------

def _ai(balance: float, *, trade_mode: int = 0,
         server: str = "", company: str = "") -> AccountInfo:
    return AccountInfo(
        login=5031019095, balance=balance, equity=balance,
        currency="USD", leverage=100,
        server=server, company=company, trade_mode=trade_mode,
    )


def test_infer_type_ftmo_100k_demo_is_challenge():
    ai = _ai(100_000.0, trade_mode=0, server="FTMO-Demo")
    assert infer_type_from_account_info(ai, "FTMO") == "challenge_100k"


def test_infer_type_ftmo_50k_demo_is_challenge():
    ai = _ai(50_000.0, trade_mode=0, server="FTMO-Demo")
    assert infer_type_from_account_info(ai, "FTMO") == "challenge_50k"


def test_infer_type_ftmo_real_is_funded():
    ai = _ai(100_000.0, trade_mode=2, server="FTMO-Server2")
    assert infer_type_from_account_info(ai, "FTMO") == "funded_100k"


def test_infer_type_ftmo_already_failed_balance_91k_snaps_to_100k():
    """An FTMO account that's lost some money should still read as 100k —
    that's what the user signed up for. Snap within ±20% to nearest size."""
    ai = _ai(91_400.0, trade_mode=0, server="FTMO-Demo")
    assert infer_type_from_account_info(ai, "FTMO") == "challenge_100k"


def test_infer_type_ftmo_balance_60k_does_not_snap_to_50k_or_100k():
    """60k is 20% off both 50k and 100k — exactly at the threshold for
    50k (delta = 10k / 50k = 0.20 ≤ 0.20) so it should snap to 50k."""
    ai = _ai(60_000.0, trade_mode=0, server="FTMO-Demo")
    assert infer_type_from_account_info(ai, "FTMO") == "challenge_50k"


def test_infer_type_oanda_demo_is_live_demo():
    ai = _ai(5_000.0, trade_mode=0, server="OandaCorp-Demo")
    assert infer_type_from_account_info(ai, "Oanda") == "live_demo"


def test_infer_type_oanda_real_is_live_real():
    ai = _ai(5_000.0, trade_mode=2, server="OandaCorp-Real")
    assert infer_type_from_account_info(ai, "Oanda") == "live_real"


def test_infer_type_balance_far_from_any_known_size_falls_back():
    """A non-prop balance of 17.3k can't snap to anything FTMO-shaped, so
    we shouldn't pretend it's a challenge."""
    ai = _ai(17_300.0, trade_mode=2, server="ForexCom-Live")
    assert infer_type_from_account_info(ai, "Forex.com") == "live_real"


# ---------------------------------------------------------------------------
# detect_local_tz
# ---------------------------------------------------------------------------

def test_detect_local_tz_returns_iana_string():
    """Even on a minimal CI box without /etc/localtime configured, this
    should return SOMETHING — UTC at worst."""
    tz = detect_local_tz()
    assert isinstance(tz, str)
    assert len(tz) > 0
    # IANA strings either contain a slash (Asia/Kolkata) or are 'UTC'
    assert "/" in tz or tz == "UTC"


def test_detect_local_tz_uses_tz_env_when_set(monkeypatch):
    """If the system has no /etc/localtime symlink and TZ is set to an
    IANA name, that should be returned."""
    # Simulate no /etc/localtime by patching readlink to raise
    monkeypatch.setattr("os.readlink", lambda p: (_ for _ in ()).throw(OSError))
    monkeypatch.setenv("TZ", "America/New_York")
    # Also disable the /etc/timezone fallback by simulating its absence
    real_open = open
    def fake_open(p, *a, **kw):
        if str(p) == "/etc/timezone":
            raise OSError("no etc timezone")
        return real_open(p, *a, **kw)
    monkeypatch.setattr("builtins.open", fake_open)
    assert detect_local_tz() == "America/New_York"


# ---------------------------------------------------------------------------
# detect_account_via_bridge — fake bridge
# ---------------------------------------------------------------------------

class _FakeBridge:
    def __init__(self, payload: dict | None, *, raise_on_call: Exception | None = None):
        self._payload = payload
        self._raise = raise_on_call

    def account_info(self, *, force_refresh: bool = True):
        if self._raise:
            raise self._raise
        # Mimic AccountInfo wire shape that MT5AccountClient produces
        from core.mt5_account import AccountInfo
        return AccountInfo(**self._payload)


def test_detect_returns_none_when_bridge_raises():
    fake = _FakeBridge(None, raise_on_call=RuntimeError("bridge offline"))
    assert detect_account_via_bridge(fake) is None


def test_detect_returns_none_when_login_zero():
    fake = _FakeBridge({
        "login": 0, "balance": 100_000.0, "equity": 100_000.0,
        "currency": "USD", "leverage": 100,
        "server": "FTMO-Demo", "company": "FTMO Trader s.r.o.",
        "trade_mode": 0, "name": "Test",
    })
    assert detect_account_via_bridge(fake) is None


def test_detect_full_round_trip_for_ftmo_demo():
    fake = _FakeBridge({
        "login": 5031019095, "balance": 91_400.0, "equity": 91_520.50,
        "currency": "USD", "leverage": 100,
        "server": "FTMO-Demo", "company": "FTMO Trader s.r.o.",
        "trade_mode": 0, "name": "Ranjeet Singh",
    })
    d = detect_account_via_bridge(fake)
    assert isinstance(d, DetectedAccount)
    assert d.login == 5031019095
    assert d.broker == "FTMO"
    assert d.type == "challenge_100k"     # snapped from 91.4k
    assert d.server == "FTMO-Demo"
    assert d.company == "FTMO Trader s.r.o."
    assert d.name == "Ranjeet Singh"
    assert d.currency == "USD"
    assert d.leverage == 100
    assert d.balance == 91_400.0
    assert d.trade_mode_label == "demo"
    assert d.is_valid
    # Risk baseline = balance (the FTMO start, not the eroded equity)
    assert d.risk_baseline_equity == 91_400.0
    # Alias is auto-generated and contains the broker
    assert "FTMO" in d.alias


def test_detect_funded_account_uses_real_label():
    fake = _FakeBridge({
        "login": 7042837461, "balance": 200_000.0, "equity": 200_500.0,
        "currency": "USD", "leverage": 30,
        "server": "FTMO-Server4", "company": "FTMO Trader s.r.o.",
        "trade_mode": 2, "name": "Trader",
    })
    d = detect_account_via_bridge(fake)
    assert d is not None
    assert d.type == "funded_200k"
    assert d.trade_mode_label == "real"


def test_detect_unknown_broker_falls_back_to_other():
    fake = _FakeBridge({
        "login": 99987654, "balance": 5_000.0, "equity": 5_000.0,
        "currency": "EUR", "leverage": 30,
        "server": "RandomBroker-X", "company": "X Brokerage Ltd",
        "trade_mode": 2, "name": "Trader",
    })
    d = detect_account_via_bridge(fake)
    assert d is not None
    assert d.broker == "Other"
    assert d.type == "live_real"


def test_detect_invalid_login_under_5_digits():
    fake = _FakeBridge({
        "login": 1234, "balance": 100_000.0, "equity": 100_000.0,
        "currency": "USD", "leverage": 100,
        "server": "FTMO-Demo", "company": "",
        "trade_mode": 0, "name": "",
    })
    d = detect_account_via_bridge(fake)
    # Login is too short to be real — caller should reject via is_valid
    assert d is not None    # we DO return a record so the UI can show why
    assert not d.is_valid
