"""
test_account_baseline_inference.py — Account.effective_baseline_equity
correctly infers the baseline from the type string when not overridden,
and respects the override when set.
"""
from __future__ import annotations

import pytest

from core import account_manager
from core.account_manager import Account


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
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


def _mk(type_: str, baseline_override: float = 0.0) -> Account:
    return Account(
        login=99999, alias="x", broker="FTMO", type=type_,
        ftmo_phase=1, user_tz="UTC", added_at_utc="2026-05-03",
        risk_baseline_equity=baseline_override,
    )


def test_baseline_inferred_from_challenge_100k():
    assert _mk("challenge_100k").effective_baseline_equity == 100_000.0


def test_baseline_inferred_from_challenge_50k():
    assert _mk("challenge_50k").effective_baseline_equity == 50_000.0


def test_baseline_inferred_from_funded_200k():
    assert _mk("funded_200k").effective_baseline_equity == 200_000.0


def test_baseline_override_wins_when_explicit():
    a = _mk("challenge_100k", baseline_override=87_500.0)
    assert a.effective_baseline_equity == 87_500.0


def test_baseline_override_zero_falls_back_to_inference():
    a = _mk("challenge_100k", baseline_override=0.0)
    assert a.effective_baseline_equity == 100_000.0


def test_baseline_inference_safe_default_for_unknown_type():
    a = _mk("custom_format_no_size")
    assert a.effective_baseline_equity == 100_000.0  # fall-back


def test_update_account_persists_new_baseline():
    account_manager.add_account(login=5031019095, alias="A",
                                  type="challenge_100k")
    a0 = account_manager.get_account(5031019095)
    assert a0.effective_baseline_equity == 100_000.0
    account_manager.update_account(5031019095,
                                    risk_baseline_equity=91_400.0)
    a1 = account_manager.get_account(5031019095)
    assert a1.effective_baseline_equity == 91_400.0
    assert a1.risk_baseline_equity == 91_400.0


def test_reset_baseline_to_current_helper():
    account_manager.add_account(login=5031019095, alias="A",
                                  type="challenge_100k")
    account_manager.reset_baseline_to_current(5031019095, 89_500.50)
    a = account_manager.get_account(5031019095)
    assert a.effective_baseline_equity == pytest.approx(89_500.50)


def test_persisted_round_trip_keeps_caps_and_target():
    account_manager.add_account(
        login=5031019095, alias="X", type="challenge_50k",
        risk_baseline_equity=42_000.0,
        daily_loss_cap_pct=4.0, total_loss_cap_pct=8.0,
        profit_target_pct=8.0, days_required=10,
    )
    a = account_manager.get_account(5031019095)
    assert a.risk_baseline_equity == 42_000.0
    assert a.daily_loss_cap_pct == 4.0
    assert a.total_loss_cap_pct == 8.0
    assert a.profit_target_pct == 8.0
    assert a.days_required == 10
    assert a.effective_baseline_equity == 42_000.0


def test_update_account_unknown_login_raises():
    import pytest
    with pytest.raises(KeyError):
        account_manager.update_account(99999, alias="nope")
