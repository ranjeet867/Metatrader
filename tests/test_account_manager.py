"""
test_account_manager.py — multi-account registry + isolation.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core import account_manager


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Each test gets its own data/accounts.json + accounts/ tree."""
    monkeypatch.setattr(account_manager, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(account_manager, "ACCOUNTS_REGISTRY",
                          tmp_path / "data" / "accounts.json")
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR",
                          tmp_path / "data" / "accounts")
    monkeypatch.setattr(account_manager, "LEGACY_DB_PATH",
                          tmp_path / "data" / "v2.db")
    monkeypatch.setattr(account_manager, "EMERGENCY_STOP_FILE",
                          tmp_path / "data" / "EMERGENCY_STOP")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)


def test_empty_registry_returns_empty_list():
    assert account_manager.list_accounts() == []


def test_add_account_creates_dir_tree():
    a = account_manager.add_account(
        login=1234567890, alias="FTMO 100k Challenge",
        broker="FTMO", type="challenge_100k", ftmo_phase=1,
        user_tz="Asia/Kolkata",
    )
    assert a.login == 1234567890
    assert a.is_ftmo
    # Directory created
    assert account_manager.account_dir(1234567890).exists()


def test_add_idempotent_on_login():
    """Adding the same login twice updates the entry — does not duplicate."""
    account_manager.add_account(login=1, alias="A", broker="FTMO",
                                 type="challenge_100k")
    account_manager.add_account(login=1, alias="A renamed",
                                 broker="FTMO", type="challenge_100k")
    accounts = account_manager.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].alias == "A renamed"


def test_get_account_returns_none_for_missing():
    assert account_manager.get_account(99999) is None


def test_get_db_path_per_account_differs_from_legacy():
    account_manager.add_account(login=1, alias="A", broker="FTMO",
                                 type="challenge_100k")
    legacy = account_manager.get_db_path(None)
    per_account = account_manager.get_db_path(1)
    assert legacy != per_account
    assert per_account.parent == account_manager.account_dir(1)


def test_remove_account_returns_true_when_present():
    account_manager.add_account(login=42, alias="X", broker="FTMO",
                                 type="challenge_100k")
    assert account_manager.remove_account(42) is True
    assert account_manager.get_account(42) is None
    # Removing again is False
    assert account_manager.remove_account(42) is False


def test_emergency_stop_lifecycle():
    assert not account_manager.emergency_stop_active()
    account_manager.touch_emergency_stop()
    assert account_manager.emergency_stop_active()
    assert account_manager.clear_emergency_stop() is True
    assert not account_manager.emergency_stop_active()
    # Clearing a non-existent file returns False
    assert account_manager.clear_emergency_stop() is False


def test_get_active_login_picks_first_active():
    account_manager.add_account(login=1, alias="A", broker="FTMO",
                                 type="challenge_100k")
    account_manager.add_account(login=2, alias="B", broker="FTMO",
                                 type="funded_100k")
    assert account_manager.get_active_login() in (1, 2)


def test_invalid_json_raises():
    p = account_manager.ACCOUNTS_REGISTRY
    p.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        account_manager.list_accounts()


def test_paths_for_per_account_artifacts():
    account_manager.add_account(login=7, alias="X", broker="FTMO",
                                 type="challenge_100k")
    assert "7" in str(account_manager.get_db_path(7))
    assert "7" in str(account_manager.get_deployments_path(7))
    assert "7" in str(account_manager.get_risk_profile_path(7))
    assert "7" in str(account_manager.get_journal_path(7))
