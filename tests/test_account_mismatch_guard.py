"""Regression tests for Bug E — account-mismatch safety guard.

The Wine MT5 bridge serves whatever account is currently logged into
MT5. If a user adds a second FTMO account in MT5 and switches active
login WITHOUT updating account_manager, the bridge will silently route
order_send / positions_get / history_deals_get to the OTHER login. Our
runner would persist trades into the configured login's per-account
DB while the actual orders execute on a different account.

The guard checks `bridge.account_info().login == self.login` at the
top of every tick. On mismatch the runner skips the entire tick (no
bar fetch, no signal, no order_send, no close-reconciliation), writes
a `bridge_alarm` row with method='account_mismatch', and logs CRITICAL.

Failure of any test below means a regression — don't weaken these.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core import storage
from core.deployment_runner import DeploymentRunner


def _build_runner(db_path: Path, *, login: int = 531019095,
                    bridge_call=None) -> DeploymentRunner:
    """Build a DeploymentRunner stub with just the fields the
    account-match check needs. Bypasses __post_init__'s state file
    loading by using __new__."""
    r = DeploymentRunner.__new__(DeploymentRunner)
    r.db_path = db_path
    r.login = login
    r.bridge_call = bridge_call
    r._last_account_mismatch_alarm_at = None
    return r


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "v2.db"
    storage.init_schema(p)
    return p


# ── _assert_account_match ──────────────────────────────────────────


def test_no_bridge_configured_returns_true(db: Path):
    """Paper-only / dry-run runners have bridge_call=None — guard is
    a no-op (returns True so the tick proceeds normally)."""
    r = _build_runner(db, bridge_call=None)
    assert r._assert_account_match() is True


def test_matching_login_returns_true(db: Path):
    """Bridge says login=531019095, runner configured for 531019095 — pass."""
    def fake_bridge(method, params):
        if method == "account_info":
            return {"ok": True, "data": {
                "login": 531019095, "balance": 91257.0, "equity": 91364.0,
                "currency": "USD", "leverage": 100, "name": "Test",
                "server": "FTMO-Server", "company": "FTMO",
                "trade_mode": 0, "margin": 0.0, "margin_free": 91257.0,
                "margin_level": 0.0,
            }}
        return {"ok": True, "data": []}
    r = _build_runner(db, login=531019095, bridge_call=fake_bridge)
    assert r._assert_account_match() is True


def test_mismatched_login_returns_false_and_writes_alarm(db: Path, caplog):
    """Bridge says login=999, runner configured for 531019095 —
    fail. Writes bridge_alarm row, logs CRITICAL, returns False."""
    def fake_bridge(method, params):
        if method == "account_info":
            return {"ok": True, "data": {
                "login": 999999999,    # SOMEONE ELSE'S ACCOUNT
                "balance": 100000.0, "equity": 100000.0,
                "currency": "USD", "leverage": 100, "name": "Other",
                "server": "FTMO-Server", "company": "FTMO",
                "trade_mode": 0, "margin": 0.0, "margin_free": 100000.0,
                "margin_level": 0.0,
            }}
        return {"ok": True, "data": []}
    r = _build_runner(db, login=531019095, bridge_call=fake_bridge)

    with caplog.at_level("CRITICAL"):
        result = r._assert_account_match()

    assert result is False
    # CRITICAL log present
    assert any("ACCOUNT MISMATCH" in rec.message for rec in caplog.records), (
        "expected CRITICAL ACCOUNT MISMATCH log entry"
    )
    # bridge_alarm row written
    with sqlite3.connect(str(db)) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM bridge_events "
            "WHERE method = 'account_mismatch'"
        ).fetchone()[0]
    assert n == 1


def test_login_zero_treated_as_mismatch(db: Path, caplog):
    """If MT5 isn't logged in at all the bridge returns login=0. Treat
    as mismatch — better to halt than trade against a logged-out terminal."""
    def fake_bridge(method, params):
        if method == "account_info":
            return {"ok": True, "data": {
                "login": 0, "balance": 0, "equity": 0,
                "currency": "", "leverage": 1, "name": "",
                "server": "", "company": "", "trade_mode": 0,
                "margin": 0.0, "margin_free": 0.0, "margin_level": 0.0,
            }}
        return {"ok": True, "data": []}
    r = _build_runner(db, login=531019095, bridge_call=fake_bridge)
    with caplog.at_level("CRITICAL"):
        assert r._assert_account_match() is False
    assert any("login=0" in rec.message for rec in caplog.records)


def test_bridge_error_treated_as_mismatch(db: Path, caplog):
    """If account_info() raises (bridge timeout, malformed response), we
    fail-CLOSED — assume mismatch and refuse the tick. Better to halt
    than silently send orders during a bridge problem."""
    def fake_bridge(method, params):
        if method == "account_info":
            raise RuntimeError("bridge timeout")
        return {"ok": True, "data": []}
    r = _build_runner(db, login=531019095, bridge_call=fake_bridge)
    with caplog.at_level("CRITICAL"):
        assert r._assert_account_match() is False
    # bridge_alarm row written even when account_info raised
    with sqlite3.connect(str(db)) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM bridge_events "
            "WHERE method = 'account_mismatch'"
        ).fetchone()[0]
    assert n == 1


def test_alarm_throttled_to_once_per_minute(db: Path):
    """Calling _assert_account_match repeatedly with the same mismatch
    must NOT spam bridge_events — throttle is 1 alarm per minute."""
    def fake_bridge(method, params):
        if method == "account_info":
            return {"ok": True, "data": {
                "login": 999, "balance": 0, "equity": 0,
                "currency": "USD", "leverage": 1, "name": "",
                "server": "", "company": "", "trade_mode": 0,
                "margin": 0.0, "margin_free": 0.0, "margin_level": 0.0,
            }}
        return {"ok": True, "data": []}
    r = _build_runner(db, login=531019095, bridge_call=fake_bridge)

    # First check — alarm written
    assert r._assert_account_match() is False
    # 12 more checks within the same minute — no new rows
    for _ in range(12):
        r._assert_account_match()
    with sqlite3.connect(str(db)) as c:
        n = c.execute(
            "SELECT COUNT(*) FROM bridge_events "
            "WHERE method = 'account_mismatch'"
        ).fetchone()[0]
    assert n == 1, f"alarm was not throttled; got {n} rows for 13 checks"


def test_alarm_error_field_carries_detail(db: Path):
    """The bridge_alarm error column must carry the diagnostic detail
    so the user can see in the dashboard what mismatched."""
    def fake_bridge(method, params):
        if method == "account_info":
            return {"ok": True, "data": {
                "login": 999999999, "balance": 0, "equity": 0,
                "currency": "USD", "leverage": 1, "name": "",
                "server": "", "company": "", "trade_mode": 0,
                "margin": 0.0, "margin_free": 0.0, "margin_level": 0.0,
            }}
        return {"ok": True, "data": []}
    r = _build_runner(db, login=531019095, bridge_call=fake_bridge)
    r._assert_account_match()
    with sqlite3.connect(str(db)) as c:
        err = c.execute(
            "SELECT error FROM bridge_events "
            "WHERE method = 'account_mismatch'"
        ).fetchone()[0]
    assert "531019095" in err
    assert "999999999" in err
