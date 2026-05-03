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
from typing import Optional


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
    # Risk anchor used by the dashboard's FTMO compliance math.
    # By default we infer the original FTMO start equity from `type`
    # (challenge_100k → 100_000, challenge_50k → 50_000, etc).
    # The user can override — useful for accounts already past the loss
    # limit being reused as test sandboxes (set to current equity to
    # re-anchor the buffer math).
    risk_baseline_equity: float = 0.0     # 0.0 means "infer from type"
    daily_loss_cap_pct: float = 5.0       # FTMO default
    total_loss_cap_pct: float = 10.0      # FTMO default
    profit_target_pct: float = 10.0       # FTMO Phase 1 default
    days_required: int = 5                # FTMO min trading days

    @property
    def is_ftmo(self) -> bool:
        return self.broker.upper() == "FTMO"

    @property
    def effective_baseline_equity(self) -> float:
        """The risk baseline the dashboard should use. If the user explicitly
        set risk_baseline_equity > 0, return it; otherwise infer from the
        account `type` string (challenge_100k → 100_000, etc)."""
        if self.risk_baseline_equity and self.risk_baseline_equity > 0:
            return float(self.risk_baseline_equity)
        # Parse trailing _<size>k from the type string
        t = (self.type or "").lower()
        for token in t.split("_"):
            if token.endswith("k") and token[:-1].isdigit():
                return float(token[:-1]) * 1000.0
        return 100_000.0  # safe default


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
            risk_baseline_equity=float(r.get("risk_baseline_equity", 0.0)),
            daily_loss_cap_pct=float(r.get("daily_loss_cap_pct", 5.0)),
            total_loss_cap_pct=float(r.get("total_loss_cap_pct", 10.0)),
            profit_target_pct=float(r.get("profit_target_pct", 10.0)),
            days_required=int(r.get("days_required", 5)),
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
            "risk_baseline_equity": a.risk_baseline_equity,
            "daily_loss_cap_pct": a.daily_loss_cap_pct,
            "total_loss_cap_pct": a.total_loss_cap_pct,
            "profit_target_pct": a.profit_target_pct,
            "days_required": a.days_required,
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
                user_tz: str = "UTC",
                risk_baseline_equity: float = 0.0,
                daily_loss_cap_pct: float = 5.0,
                total_loss_cap_pct: float = 10.0,
                profit_target_pct: float = 10.0,
                days_required: int = 5) -> Account:
    """Add a new account. Idempotent on `login` — existing entry updated.
    Creates the data/accounts/{login}/ tree.

    risk_baseline_equity = 0.0 means "infer from type" (recommended for
    fresh challenges). For accounts already past the loss limit being
    reused as sandboxes, set this to the current equity.
    """
    accounts = _load_registry()
    new = Account(login=int(login), alias=alias, broker=broker, type=type,
                   ftmo_phase=int(ftmo_phase), user_tz=user_tz,
                   added_at_utc=datetime.now(timezone.utc).isoformat(),
                   risk_baseline_equity=float(risk_baseline_equity),
                   daily_loss_cap_pct=float(daily_loss_cap_pct),
                   total_loss_cap_pct=float(total_loss_cap_pct),
                   profit_target_pct=float(profit_target_pct),
                   days_required=int(days_required))
    accounts = [a for a in accounts if a.login != new.login] + [new]
    _save_registry(accounts)
    _ensure_dirs(new.login)
    return new


def update_account(login: int, **changes) -> Account:
    """Mutate fields on an existing account. Returns the updated record.
    Raises KeyError if login is unknown."""
    accounts = _load_registry()
    found = None
    for i, a in enumerate(accounts):
        if a.login == login:
            found = i
            break
    if found is None:
        raise KeyError(f"login {login} not in registry")
    cur = accounts[found]
    new_fields = {
        "login": cur.login, "alias": cur.alias, "broker": cur.broker,
        "type": cur.type, "ftmo_phase": cur.ftmo_phase,
        "user_tz": cur.user_tz, "added_at_utc": cur.added_at_utc,
        "active": cur.active,
        "risk_baseline_equity": cur.risk_baseline_equity,
        "daily_loss_cap_pct": cur.daily_loss_cap_pct,
        "total_loss_cap_pct": cur.total_loss_cap_pct,
        "profit_target_pct": cur.profit_target_pct,
        "days_required": cur.days_required,
    }
    new_fields.update(changes)
    accounts[found] = Account(**new_fields)
    _save_registry(accounts)
    return accounts[found]


def reset_baseline_to_current(login: int, current_equity: float) -> Account:
    """Convenience: set risk_baseline_equity to current_equity. Used when
    re-anchoring an already-blown account as a sandbox."""
    return update_account(login, risk_baseline_equity=float(current_equity))


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
