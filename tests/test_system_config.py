"""
test_system_config.py — pin the SystemConfig load contract.

These tests prove the foundation invariants:
  - Every field has a defined default; load with no JSON files returns
    a valid SystemConfig (no None, no KeyError).
  - Cache TTL works: two reads within 60s are the same object.
  - invalidate(login) forces a fresh read.
  - Per-account isolation: two logins get independent configs.
  - Schema version is set.
  - Deployment caps roundtrip cleanly from deployments.json.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from core import account_manager, deployment as dep_mod, system_config
from core.deployment import Deployment


@pytest.fixture
def tmp_account(tmp_path, monkeypatch):
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR", accounts_dir)
    monkeypatch.setattr(
        account_manager, "ACCOUNTS_REGISTRY",
        tmp_path / "accounts.json",
    )
    # Reset module cache so each test starts clean
    system_config._cache.clear()
    return 100


def test_load_returns_valid_struct(tmp_account):
    """No deployments.json on disk → empty deployment_caps but valid struct."""
    login = tmp_account
    cfg = system_config.load_system_config(login)

    assert cfg.login == login
    assert cfg.schema_version == 1
    assert cfg.cost_model.commission_usd > 0
    assert cfg.account_risk.daily_loss_cap_pct > 0
    assert cfg.deployment_caps_by_id == {}


def test_cache_ttl_returns_same_struct(tmp_account):
    """Two reads within 60s → same struct (object identity)."""
    login = tmp_account
    cfg1 = system_config.load_system_config(login)
    cfg2 = system_config.load_system_config(login)
    assert cfg1 is cfg2


def test_invalidate_forces_fresh_read(tmp_account):
    """After invalidate(login), next read produces a new struct."""
    login = tmp_account
    cfg1 = system_config.load_system_config(login)
    system_config.invalidate(login)
    cfg2 = system_config.load_system_config(login)
    # Different object (loaded_at_unix differs at minimum)
    assert cfg1 is not cfg2


def test_per_account_isolation(tmp_account, tmp_path, monkeypatch):
    """Two logins get separate cached configs."""
    login_a = tmp_account
    login_b = login_a + 1
    cfg_a = system_config.load_system_config(login_a)
    cfg_b = system_config.load_system_config(login_b)
    assert cfg_a.login == login_a
    assert cfg_b.login == login_b
    assert cfg_a is not cfg_b


def test_deployment_caps_roundtrip(tmp_account):
    """Deployment created via dep_mod is visible to system_config."""
    login = tmp_account
    dep = Deployment(
        deployment_id="rsi_30_70_EURUSD_M15_long",
        strategy="rsi_30_70",
        ticker="EURUSD",
        tf="M15",
        long_only=True,
        risk_pct=0.5,
        daily_cap_pct=2.0,
        max_lots=10.0,
        max_money_risk_usd=750.0,
        status="live",
    )
    dep_mod.save_deployments(login, [dep])
    system_config.invalidate(login)

    cfg = system_config.load_system_config(login)
    caps = cfg.deployment_caps("rsi_30_70_EURUSD_M15_long")
    assert caps.risk_pct == pytest.approx(0.5)
    assert caps.daily_cap_pct == pytest.approx(2.0)
    assert caps.max_lots == pytest.approx(10.0)
    assert caps.max_money_risk_usd == pytest.approx(750.0)


def test_deployment_caps_or_default_returns_default(tmp_account):
    """For an unknown deployment_id, return a sensible default."""
    login = tmp_account
    cfg = system_config.load_system_config(login)
    caps = cfg.deployment_caps_or_default("nonexistent")
    assert caps.risk_pct > 0
    assert caps.max_lots > 0


def test_summary_dict_has_unified_keys(tmp_account):
    """get_summary_dict returns a flat dict with namespaced keys.
    This is what the unified Settings UI page reads."""
    login = tmp_account
    summary = system_config.get_summary_dict(login)
    assert "cost.commission_usd" in summary
    assert "risk.daily_loss_cap_pct" in summary
    assert "risk.max_open_positions" in summary
    assert "n_deployments" in summary


def test_convenience_accessors(tmp_account):
    """daily_loss_cap_pct(login) etc. return the same value as
    accessing the struct directly."""
    login = tmp_account
    cfg = system_config.load_system_config(login)
    assert (system_config.daily_loss_cap_pct(login)
            == cfg.account_risk.daily_loss_cap_pct)
    assert (system_config.max_open_positions(login)
            == cfg.account_risk.max_open_positions)
