"""
test_runner_weekend_flat.py — pin the contract that DeploymentRunner
force-closes broker positions when the weekend-flat window fires.

Pre-fix the live runner had no weekend-flat enforcement at all.
backtest + paper_executor closed positions at Friday 19:55 UTC, but
the live runner just kept ticking — a Friday position would sit
through the weekend with full broker exposure to Sunday-night gaps.

This test:
  - Constructs a runner pointed at an in-memory bridge mock
  - Mocks `positions_get()` to return one open position
  - Calls `_force_flat_if_close_window` with a Friday 19:55 UTC `now`
  - Asserts the bridge received a `close_position` call
  - Asserts a `close:weekend_flat` row was written to bridge_events
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import pytest

from core import account_manager, deployment as dep_mod, storage
from core.deployment import Deployment
from core.deployment_runner import DeploymentRunner


@pytest.fixture
def tmp_account(tmp_path, monkeypatch):
    """Set up a tmp account dir + writable v2.db under it."""
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    # Patch the per-account paths so dep_mod / storage write into tmp_path
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(
        account_manager, "ACCOUNTS_REGISTRY",
        tmp_path / "accounts.json",
    )
    login = 999
    acct_dir = accounts_dir / str(login)
    acct_dir.mkdir()
    db_path = acct_dir / "v2.db"
    storage.init_schema(db_path)
    # One live deployment
    dep = Deployment(
        deployment_id="rsi_30_70_EURUSD_M15_long",
        strategy="rsi_30_70", ticker="EURUSD", tf="M15",
        long_only=True, status="live",
    )
    dep_mod.save_deployments(login, [dep])
    return login, db_path


def _friday_close_window_utc() -> datetime:
    """Friday at 19:55 UTC — within the default close-window
    (us_session_close 20:00, flat_buffer 5min)."""
    # 2026-05-01 was a Friday
    return datetime(2026, 5, 1, 19, 55, tzinfo=timezone.utc)


def test_force_flat_closes_open_position_on_friday_close(tmp_account):
    login, db_path = tmp_account

    # Mock bridge: one open EURUSD position, close returns ok
    fake_position = MagicMock()
    fake_position.symbol = "EURUSD"
    fake_position.ticket = 12345
    fake_position.type = 0          # 0 = LONG in MT5
    fake_position.volume = 0.5
    fake_position.time_setup = "2026-05-01T18:00:00+00:00"

    bridge_calls: list[dict] = []

    def bridge_mock(payload: dict) -> dict:
        bridge_calls.append(payload)
        if payload.get("method") == "close_position":
            return {"ok": True, "ticket": payload["ticket"]}
        return {"ok": False, "error": "unexpected method"}

    runner = DeploymentRunner(
        login=login,
        candle_fetcher=lambda *a, **k: None,   # not used
        bridge_call=bridge_mock,
        poll_seconds=60,
        db_path=db_path,
    )

    # Patch positions_get to return our fake position
    from core import mt5_account
    monkey = MagicMock(return_value=[fake_position])
    original = mt5_account.MT5AccountClient.positions_get
    mt5_account.MT5AccountClient.positions_get = monkey
    try:
        deps = dep_mod.load_deployments(login)
        runner._force_flat_if_close_window(
            _friday_close_window_utc().isoformat(),
            deps,
        )
    finally:
        mt5_account.MT5AccountClient.positions_get = original

    # Assert: the bridge got a close_position call
    close_calls = [c for c in bridge_calls
                    if c.get("method") == "close_position"]
    assert len(close_calls) == 1, (
        f"expected exactly 1 close_position call; got {bridge_calls}"
    )
    assert close_calls[0]["ticket"] == 12345

    # Assert: a close:weekend_flat row landed in bridge_events
    with sqlite3.connect(str(db_path)) as c:
        rows = c.execute(
            "SELECT method, ok FROM bridge_events "
            "WHERE method LIKE 'close:%'"
        ).fetchall()
    assert any(m == "close:weekend_flat" and ok == 1 for m, ok in rows), (
        f"expected close:weekend_flat row in bridge_events; got {rows}"
    )


def test_force_flat_noop_outside_close_window(tmp_account):
    """Tuesday 10am UTC is well outside any close window → no-op."""
    login, db_path = tmp_account

    bridge_calls: list[dict] = []

    def bridge_mock(payload: dict) -> dict:
        bridge_calls.append(payload)
        return {"ok": True}

    runner = DeploymentRunner(
        login=login,
        candle_fetcher=lambda *a, **k: None,
        bridge_call=bridge_mock,
        poll_seconds=60,
        db_path=db_path,
    )

    # Patch with a single open position
    fake_position = MagicMock()
    fake_position.symbol = "EURUSD"
    fake_position.ticket = 999
    fake_position.type = 0
    fake_position.volume = 0.1
    fake_position.time_setup = "2026-04-28T10:00:00+00:00"

    from core import mt5_account
    monkey = MagicMock(return_value=[fake_position])
    original = mt5_account.MT5AccountClient.positions_get
    mt5_account.MT5AccountClient.positions_get = monkey
    try:
        deps = dep_mod.load_deployments(login)
        # Tuesday 10:00 UTC — far from any close window
        tuesday = datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc)
        runner._force_flat_if_close_window(tuesday.isoformat(), deps)
    finally:
        mt5_account.MT5AccountClient.positions_get = original

    close_calls = [c for c in bridge_calls
                    if c.get("method") == "close_position"]
    assert len(close_calls) == 0, (
        f"unexpectedly closed position outside close window: {bridge_calls}"
    )


def test_force_flat_skips_when_no_bridge(tmp_account, caplog):
    """If bridge_call is None (e.g. dashboard runner with no MT5),
    skip silently — never crash."""
    login, db_path = tmp_account

    runner = DeploymentRunner(
        login=login,
        candle_fetcher=lambda *a, **k: None,
        bridge_call=None,
        poll_seconds=60,
        db_path=db_path,
    )

    deps = dep_mod.load_deployments(login)
    # Should not raise
    runner._force_flat_if_close_window(
        _friday_close_window_utc().isoformat(), deps,
    )
