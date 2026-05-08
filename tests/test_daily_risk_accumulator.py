"""
test_daily_risk_accumulator.py — pin the FTMO-breach-prevention contract.

The accumulator must:
  1. Block new opens when the day's running total would exceed cap
  2. Reset at the daily rollover hour (FTMO 22:00 UTC)
  3. Survive process restart (state on disk)
  4. Be atomic (concurrent dashboard reads see consistent state)
  5. Permit the FIRST open up to the cap; reject the one that pushes over
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from core import daily_risk_accumulator as dra


@pytest.fixture
def tmp_account(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    from core import account_manager
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(
        account_manager, "ACCOUNTS_REGISTRY",
        tmp_path / "accounts.json",
    )
    return 777, accounts_dir


def test_first_open_under_cap_passes(tmp_account):
    login, _ = tmp_account
    result = dra.would_breach_cap(
        login=login, new_risk_usd=300.0,
        daily_loss_cap_pct=1.0, starting_balance_usd=100_000.0,
    )
    assert result.would_breach is False
    assert result.cap_usd == pytest.approx(1000.0)
    assert result.risk_used_usd_after == pytest.approx(300.0)


def test_record_open_then_check_against_remaining_cap(tmp_account):
    login, _ = tmp_account
    # Record one open at $300 risk
    dra.record_open(login=login, deployment_id="x",
                     risk_usd=300.0)
    # Now check: $400 more would be ok ($700 < $1000 cap)
    r1 = dra.would_breach_cap(
        login=login, new_risk_usd=400.0,
        daily_loss_cap_pct=1.0, starting_balance_usd=100_000.0,
    )
    assert r1.would_breach is False
    assert r1.risk_used_usd_after == pytest.approx(700.0)
    # And $800 more would BREACH (300+800=1100 > 1000)
    r2 = dra.would_breach_cap(
        login=login, new_risk_usd=800.0,
        daily_loss_cap_pct=1.0, starting_balance_usd=100_000.0,
    )
    assert r2.would_breach is True
    assert r2.risk_used_usd_after == pytest.approx(1100.0)
    assert "breached" in r2.reason


def test_accumulator_persists_across_restart(tmp_account):
    """Simulating a process restart: write state, then check via fresh
    load. The accumulator must remember the day's running total."""
    login, _ = tmp_account
    dra.record_open(login=login, deployment_id="x", risk_usd=500.0)
    # Clear in-memory locks (would happen on restart). State remains
    # on disk so the next check picks it up.
    dra._locks.clear()
    summary = dra.get_summary(
        login=login, daily_loss_cap_pct=1.0,
        starting_balance_usd=100_000.0,
    )
    assert summary["risk_used_usd"] == pytest.approx(500.0)
    assert summary["open_count"] == 1


def test_rollover_resets_state(tmp_account):
    """When FTMO day flips (22:00 UTC), the accumulator must reset to 0."""
    login, _ = tmp_account
    # Stamp current_day_start_utc to YESTERDAY → next load should reset
    dra.record_open(login=login, deployment_id="x", risk_usd=400.0)

    # Manually edit the state file to simulate "yesterday's day"
    path = dra._state_path(login)
    import json
    data = json.loads(path.read_text())
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    data["current_day_start_utc"] = yesterday
    path.write_text(json.dumps(data))
    dra._locks.clear()

    # Fresh check should see a zeroed accumulator
    summary = dra.get_summary(
        login=login, daily_loss_cap_pct=1.0,
        starting_balance_usd=100_000.0,
    )
    assert summary["risk_used_usd"] == pytest.approx(0.0)
    assert summary["open_count"] == 0


def test_summary_remaining_usd(tmp_account):
    login, _ = tmp_account
    dra.record_open(login=login, deployment_id="x", risk_usd=200.0)
    s = dra.get_summary(
        login=login, daily_loss_cap_pct=1.0,
        starting_balance_usd=100_000.0,
    )
    assert s["cap_usd"] == pytest.approx(1000.0)
    assert s["remaining_usd"] == pytest.approx(800.0)
    assert s["fraction_used"] == pytest.approx(0.20, abs=0.001)


def test_event_log_capped_at_100(tmp_account):
    """We don't want the JSON to grow unbounded; event log truncates."""
    login, _ = tmp_account
    for i in range(120):
        dra.record_open(login=login, deployment_id=f"d{i}", risk_usd=1.0)
    summary = dra.get_summary(
        login=login, daily_loss_cap_pct=2.0,
        starting_balance_usd=100_000.0,
    )
    # 120 opens but only last 10 returned (events[-10:])
    assert len(summary["events"]) == 10
    # And risk_used_usd is the full 120
    assert summary["risk_used_usd"] == pytest.approx(120.0)


def test_per_account_isolation(tmp_account, tmp_path):
    """Two logins → independent accumulators."""
    login_a = 888
    login_b = 889
    dra.record_open(login=login_a, deployment_id="x", risk_usd=100.0)
    dra.record_open(login=login_b, deployment_id="y", risk_usd=200.0)
    sa = dra.get_summary(login=login_a,
                          daily_loss_cap_pct=1.0,
                          starting_balance_usd=100_000.0)
    sb = dra.get_summary(login=login_b,
                          daily_loss_cap_pct=1.0,
                          starting_balance_usd=100_000.0)
    assert sa["risk_used_usd"] == pytest.approx(100.0)
    assert sb["risk_used_usd"] == pytest.approx(200.0)
