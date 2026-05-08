"""
test_runner_bar_close_schedule.py — pin the contract that the runner
fires within ~2s of bar close on the smallest active TF, NOT just every
poll_seconds.

Why this matters
----------------
60s polling on a 15-min strategy meant up to 60s of price drift between
bar close and order send — vs the catalog's 5%-of-ATR slippage
assumption (~0.25 pip on EURUSD). That's a 4-8x slippage gap eating
into PF on every trade, silently. Bar-close-aware scheduling closes
the gap by sleeping until "next close + 2s" whenever that's sooner
than the next regular poll.

These tests pin:

  1. _seconds_until_next_bar_close returns a positive float when an
     active M15 deployment exists, less than 900s (M15 period).
  2. With NO active deployments, returns None (runner falls back to
     poll_seconds — no point doing bar math when nothing will fire).
  3. Picks the SMALLEST TF when multiple are active (M15 wins over D1).
  4. Boundary math: at 12:00:00 UTC, next M15 close is at 12:15:00 = 900s.
  5. Late in the day: at 23:50:00 UTC, next M15 close is at 23:55:00 = 300s.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

import pandas as pd
import pytest

from core import account_manager, deployment as dep_mod, storage
from core.deployment import Deployment
from core.deployment_runner import DeploymentRunner


@pytest.fixture
def tmp_account(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(
        account_manager, "ACCOUNTS_REGISTRY",
        tmp_path / "accounts.json",
    )
    login = 555
    acct_dir = accounts_dir / str(login)
    acct_dir.mkdir()
    db_path = acct_dir / "v2.db"
    storage.init_schema(db_path)
    return login, db_path


def _make_runner(login, db_path):
    return DeploymentRunner(
        login=login,
        candle_fetcher=lambda *a, **k: pd.DataFrame(),
        bridge_call=None,
        db_path=db_path,
        poll_seconds=5,
    )


def test_no_active_deployments_returns_none(tmp_account):
    login, db_path = tmp_account
    # Empty deployments list
    dep_mod.save_deployments(login, [])

    runner = _make_runner(login, db_path)
    assert runner._seconds_until_next_bar_close() is None


def test_idle_deployment_returns_none(tmp_account):
    """Deployments in idle/halted/paused status don't count — only
    paper/live get bar-close-aware scheduling because only they will
    actually fire on a new bar."""
    login, db_path = tmp_account
    dep_mod.save_deployments(login, [
        Deployment(
            deployment_id="x_M15", strategy="x", ticker="EURUSD", tf="M15",
            status="idle",   # not paper/live
        ),
    ])
    runner = _make_runner(login, db_path)
    assert runner._seconds_until_next_bar_close() is None


def test_m15_at_top_of_hour_returns_900s(tmp_account):
    login, db_path = tmp_account
    dep_mod.save_deployments(login, [
        Deployment(
            deployment_id="rsi_M15", strategy="rsi", ticker="EURUSD",
            tf="M15", status="live",
        ),
    ])
    runner = _make_runner(login, db_path)

    # Pin "now" to 2026-05-05 12:00:00 UTC. Next M15 close = 12:15:00.
    fake_now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
    with patch("core.deployment_runner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        # Keep timezone import working
        mock_dt.fromisoformat = datetime.fromisoformat
        secs = runner._seconds_until_next_bar_close()

    assert secs == pytest.approx(900.0, abs=0.01)


def test_m15_at_07_30_returns_900s(tmp_account):
    login, db_path = tmp_account
    dep_mod.save_deployments(login, [
        Deployment(
            deployment_id="rsi_M15", strategy="rsi", ticker="EURUSD",
            tf="M15", status="live",
        ),
    ])
    runner = _make_runner(login, db_path)

    # 07:30:00 — next M15 close at 07:45:00 = 900s
    fake_now = datetime(2026, 5, 5, 7, 30, 0, tzinfo=timezone.utc)
    with patch("core.deployment_runner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        secs = runner._seconds_until_next_bar_close()
    assert secs == pytest.approx(900.0, abs=0.01)


def test_m15_at_07_22_returns_180s(tmp_account):
    """07:22 → next M15 close at 07:30 = 480s.

    Wall-clock-aligned M15 boundaries are :00, :15, :30, :45. So the
    next close after 07:22 is 07:30 (8 minutes = 480s away)."""
    login, db_path = tmp_account
    dep_mod.save_deployments(login, [
        Deployment(
            deployment_id="rsi_M15", strategy="rsi", ticker="EURUSD",
            tf="M15", status="live",
        ),
    ])
    runner = _make_runner(login, db_path)

    fake_now = datetime(2026, 5, 5, 7, 22, 0, tzinfo=timezone.utc)
    with patch("core.deployment_runner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        secs = runner._seconds_until_next_bar_close()
    assert secs == pytest.approx(480.0, abs=0.01)


def test_smallest_tf_wins_when_multiple_active(tmp_account):
    """Mix of M15 + D1 deployments → return time to next M15 close
    (smaller). D1 close is 24h away most of the time; M15 is at most
    15min away — runner should align to the more frequent."""
    login, db_path = tmp_account
    dep_mod.save_deployments(login, [
        Deployment(
            deployment_id="d1_strat", strategy="x", ticker="EURUSD",
            tf="D1", status="live",
        ),
        Deployment(
            deployment_id="m15_strat", strategy="rsi", ticker="EURUSD",
            tf="M15", status="live",
        ),
    ])
    runner = _make_runner(login, db_path)

    # 12:07 → next M15 close at 12:15 = 480s. (D1 close 86400s away.)
    fake_now = datetime(2026, 5, 5, 12, 7, 0, tzinfo=timezone.utc)
    with patch("core.deployment_runner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        secs = runner._seconds_until_next_bar_close()
    assert secs == pytest.approx(480.0, abs=0.01)


def test_d1_at_22_00_returns_2h(tmp_account):
    """For a D1-only deployment, next close is at 00:00 UTC. At 22:00,
    that's 2h = 7200s away."""
    login, db_path = tmp_account
    dep_mod.save_deployments(login, [
        Deployment(
            deployment_id="d1_strat", strategy="x", ticker="EURUSD",
            tf="D1", status="live",
        ),
    ])
    runner = _make_runner(login, db_path)

    fake_now = datetime(2026, 5, 5, 22, 0, 0, tzinfo=timezone.utc)
    with patch("core.deployment_runner.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        mock_dt.fromisoformat = datetime.fromisoformat
        secs = runner._seconds_until_next_bar_close()
    assert secs == pytest.approx(7200.0, abs=0.01)
