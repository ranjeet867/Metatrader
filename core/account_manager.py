"""
account_manager.py — multi-account registry + per-account paths.

Phase 2 introduces "Operations" page that lets the user run the same set of
strategies against multiple FTMO / live accounts. Each account has its own:
  - trade journal (SQLite)
  - deployments.json
  - risk_profile.json
  - symbol_info.json

The candle data (data/*.parquet) is SHARED — broker price history is universal.
The EMERGENCY_STOP file is SHARED — pulling the stop affects every account.

INVARIANT-9 (account isolation): A's trades never appear in B's journal.

A registry file at data/accounts.json lists configured accounts. There is
ALWAYS a "default" pseudo-account at data/v2.db for backwards compatibility
with everything Phase 1 built — Phase 2 modules call AccountManager.get_db_path
which returns the legacy path if no account-specific dir exists.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_REGISTRY = REPO_ROOT / "data" / "accounts.json"
ACCOUNTS_DIR = REPO_ROOT / "data" / "accounts"
LEGACY_DB_PATH = REPO_ROOT / "data" / "v2.db"        # Phase 1 single-account
EMERGENCY_STOP_FILE = REPO_ROOT / "data" / "EMERGENCY_STOP"


@dataclass(frozen=True)
class Account:
    login: int
    alias: str
    broker: str
    type: str               # 'challenge_100k', 'verification_100k', 'funded_100k', 'live_demo', 'live_real'
    ftmo_phase: int         # 1, 2, or 0 (funded / non-FTMO)
    user_tz: str            # IANA tz e.g. 'Asia/Kolkata'
    added_at_utc: str
    active: bool = True

    @property
    def is_ftmo(self) -> bool:
        return self.broker.upper() == "FTMO"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_registry() -> list[Account]:
    if not ACCOUNTS_REGISTRY.exists():
        return []
    try:
        rows = json.loads(ACCOUNTS_REGISTRY.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"accounts.json is not valid JSON: {e}") from e
    if not isinstance(rows, list):
        raise ValueError("accounts.json must be a JSON array")
    out = []
    for r in rows:
        out.append(Account(
            login=int(r["login"]),
            alias=str(r["alias"]),
            broker=str(r.get("broker", "FTMO")),
            type=str(r.get("type", "challenge_100k")),
            ftmo_phase=int(r.get("ftmo_phase", 1)),
            user_tz=str(r.get("user_tz", "UTC")),
            added_at_utc=str(r.get("added_at_utc",
                                   datetime.now(timezone.utc).isoformat())),
            active=bool(r.get("active", True)),
        ))
    return out


def _save_registry(accounts: list[Account]) -> None:
    ACCOUNTS_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "login": a.login, "alias": a.alias, "broker": a.broker,
            "type": a.type, "ftmo_phase": a.ftmo_phase,
            "user_tz": a.user_tz, "added_at_utc": a.added_at_utc,
            "active": a.active,
        }
        for a in accounts
    ]
    ACCOUNTS_REGISTRY.write_text(json.dumps(payload, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_accounts() -> list[Account]:
    return _load_registry()


def get_account(login: int) -> Optional[Account]:
    for a in _load_registry():
        if a.login == login:
            return a
    return None


def add_account(*, login: int, alias: str, broker: str = "FTMO",
                type: str = "challenge_100k", ftmo_phase: int = 1,
                user_tz: str = "UTC") -> Account:
    """Add a new account. Idempotent on `login` — existing entry updated.
    Creates the data/accounts/{login}/ tree."""
    accounts = _load_registry()
    new = Account(login=int(login), alias=alias, broker=broker, type=type,
                   ftmo_phase=int(ftmo_phase), user_tz=user_tz,
                   added_at_utc=datetime.now(timezone.utc).isoformat())
    accounts = [a for a in accounts if a.login != new.login] + [new]
    _save_registry(accounts)
    _ensure_dirs(new.login)
    return new


def remove_account(login: int) -> bool:
    """Remove from registry. Does NOT delete the account directory (operator
    can keep / archive). Returns True if removed."""
    accounts = _load_registry()
    new_list = [a for a in accounts if a.login != login]
    if len(new_list) == len(accounts):
        return False
    _save_registry(new_list)
    return True


def account_dir(login: int) -> Path:
    return ACCOUNTS_DIR / str(login)


def get_db_path(login: int | None) -> Path:
    """Per-account SQLite path. Returns the legacy data/v2.db when login is
    None (preserves Phase 1 behaviour for code paths that haven't migrated)."""
    if login is None:
        return LEGACY_DB_PATH
    _ensure_dirs(login)
    return account_dir(login) / "v2.db"


def get_deployments_path(login: int) -> Path:
    _ensure_dirs(login)
    return account_dir(login) / "deployments.json"


def get_risk_profile_path(login: int) -> Path:
    _ensure_dirs(login)
    return account_dir(login) / "risk_profile.json"


def get_symbol_info_path(login: int) -> Path:
    """Per-account symbol_info path. Falls back to the SHARED data/symbol_info.json
    when the per-account file does not exist (most users will share)."""
    _ensure_dirs(login)
    per_account = account_dir(login) / "symbol_info.json"
    return per_account if per_account.exists() else (
        REPO_ROOT / "data" / "symbol_info.json"
    )


def get_journal_path(login: int) -> Path:
    """Append-only audit JSONL per account."""
    _ensure_dirs(login)
    return account_dir(login) / "journal.jsonl"


def get_active_login(default: int | None = None) -> int | None:
    """The 'active' login is determined by the dashboard's session_state.
    For non-UI callers (cron, scripts, tests), use the first active account
    in the registry, or `default` if none configured."""
    for a in _load_registry():
        if a.active:
            return a.login
    return default


def emergency_stop_active() -> bool:
    return EMERGENCY_STOP_FILE.exists()


def touch_emergency_stop() -> None:
    EMERGENCY_STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMERGENCY_STOP_FILE.touch()


def clear_emergency_stop() -> bool:
    if EMERGENCY_STOP_FILE.exists():
        EMERGENCY_STOP_FILE.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Directory hygiene
# ---------------------------------------------------------------------------

def _ensure_dirs(login: int) -> None:
    d = account_dir(login)
    d.mkdir(parents=True, exist_ok=True)
