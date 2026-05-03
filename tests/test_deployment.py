"""
test_deployment.py — Deployment + per-account persistence.
"""
from __future__ import annotations

import pytest

from core import account_manager
from core.deployment import (
    Deployment,
    load_deployments,
    remove_deployment,
    save_deployments,
    seed_survivor_deployments,
    update_status,
    upsert_deployment,
)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(account_manager, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(account_manager, "ACCOUNTS_REGISTRY",
                          tmp_path / "data" / "accounts.json")
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR",
                          tmp_path / "data" / "accounts")
    (tmp_path / "data").mkdir()


def test_round_trip():
    dep = Deployment(deployment_id="x_y_z", strategy="vol_breakout",
                      ticker="US100.cash", tf="D1",
                      long_only=True, params={"long_only": True},
                      risk_pct=0.3, daily_cap_pct=1.0)
    save_deployments(1, [dep])
    loaded = load_deployments(1)
    assert len(loaded) == 1
    assert loaded[0].deployment_id == "x_y_z"
    assert loaded[0].strategy == "vol_breakout"


def test_upsert_replaces_by_id():
    a = Deployment(deployment_id="x", strategy="s", ticker="T", tf="D1",
                    risk_pct=0.3)
    b = Deployment(deployment_id="x", strategy="s", ticker="T", tf="D1",
                    risk_pct=0.5)        # same id, different risk
    upsert_deployment(1, a)
    upsert_deployment(1, b)
    deps = load_deployments(1)
    assert len(deps) == 1
    assert deps[0].risk_pct == 0.5


def test_remove():
    upsert_deployment(1, Deployment(deployment_id="x", strategy="s",
                                       ticker="T", tf="D1"))
    assert remove_deployment(1, "x") is True
    assert load_deployments(1) == []
    assert remove_deployment(1, "x") is False


def test_update_status():
    upsert_deployment(1, Deployment(deployment_id="x", strategy="s",
                                       ticker="T", tf="D1"))
    d = update_status(1, "x", "live", live_run_id="LR_001")
    assert d is not None
    assert d.status == "live"
    assert d.live_run_id == "LR_001"
    assert d.last_started_at_utc is not None


def test_seed_survivors_idempotent():
    """Second call must return the same set as the first — no growth.

    Count isn't pinned because the seed list is derived from
    core.strategy_library (when grid_results.md is present) or from a
    static fallback otherwise. What matters is idempotency."""
    n1 = len(seed_survivor_deployments(1))
    n2 = len(seed_survivor_deployments(1))    # second call adds nothing
    assert n1 == n2
    assert n1 >= 4


def test_slug_filesystem_safe():
    s = Deployment.slug("vol_breakout", "US100.cash", "D1")
    assert "." not in s
    assert s == "vol_breakout_US100_cash_D1"
