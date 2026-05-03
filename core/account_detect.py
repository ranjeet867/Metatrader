"""
account_detect.py — auto-detect the connected MT5 account so the user
doesn't have to type their login.

Pulls AccountInfo from the running bridge, infers the broker from the
server string, snaps balance to the nearest known FTMO size, and detects
the user's local IANA timezone from /etc/localtime.

Used by the dashboard's "🔍 Auto-detect from MT5" button.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from core.mt5_account import AccountInfo, MT5AccountClient


# ---------------------------------------------------------------------------
# Broker server inference
# ---------------------------------------------------------------------------
#
# MT5 servers are named like "<Broker>-<flavor>" (e.g. "FTMO-Demo",
# "FTMO-Server2", "TopStepFutures-Demo"). We map known prefixes to the
# canonical broker tag the dashboard uses.
_BROKER_PATTERNS: list[tuple[str, str]] = [
    ("ftmo",            "FTMO"),
    ("topstep",         "TopStep"),
    ("myforexfunds",    "MyForexFunds"),
    ("the5ers",         "The5ers"),
    ("fundednext",      "FundedNext"),
    ("e8",              "E8"),
    ("smartprop",       "SmartProp"),
    ("forex.com",       "Forex.com"),
    ("oanda",           "Oanda"),
    ("ic markets",      "IC Markets"),
    ("icmarkets",       "IC Markets"),
    ("pepperstone",     "Pepperstone"),
]


def infer_broker_from_server(server: str, *, company: str = "") -> str:
    """Best-effort broker name from server / company strings. Falls back
    to 'Other'."""
    for src in (server or "", company or ""):
        s = src.lower()
        for needle, tag in _BROKER_PATTERNS:
            if needle in s:
                return tag
    return "Other"


# ---------------------------------------------------------------------------
# FTMO challenge size inference
# ---------------------------------------------------------------------------
#
# FTMO balances are 10k / 25k / 50k / 100k / 200k. Snap to the nearest
# size when the balance is within ±10% of a known step (so a $91,400
# account that's already failed reads as 100k, not 90k).
_FTMO_SIZES_K: list[int] = [10, 25, 50, 100, 200]


def _nearest_ftmo_size_k(balance: float) -> int | None:
    """Snap to the nearest FTMO size if within ±20%, else None."""
    if balance <= 0:
        return None
    best = None
    best_dev = float("inf")
    for k in _FTMO_SIZES_K:
        size = k * 1000.0
        dev = abs(balance - size) / size
        if dev < best_dev:
            best, best_dev = k, dev
    if best_dev <= 0.20:
        return best
    return None


def infer_type_from_account_info(ai: AccountInfo, broker: str) -> str:
    """Best-effort 'challenge_100k' / 'funded_50k' / 'live_real' choice
    from AccountInfo.

    Heuristic:
      - If broker is a prop firm and trade_mode == DEMO, treat as challenge.
      - If broker is a prop firm and trade_mode == REAL, treat as funded.
      - If broker is not a prop firm, use live_demo / live_real based on mode.
    """
    is_prop = broker in {"FTMO", "TopStep", "MyForexFunds", "The5ers",
                          "FundedNext", "E8", "SmartProp"}
    size_k = _nearest_ftmo_size_k(ai.balance)
    if is_prop and size_k is not None:
        if ai.trade_mode == 2:  # REAL → funded account
            return f"funded_{size_k}k"
        # DEMO or CONTEST → challenge / verification (we can't tell which
        # without phase context, default to challenge — user can override)
        return f"challenge_{size_k}k"
    # Not a prop firm or unknown size
    return "live_real" if ai.trade_mode == 2 else "live_demo"


# ---------------------------------------------------------------------------
# Local timezone detection
# ---------------------------------------------------------------------------

def detect_local_tz() -> str:
    """Best-effort IANA timezone from the host. Returns 'UTC' on failure.

    Order of attempts:
      1. /etc/localtime symlink (macOS / most Linux distros)
      2. TZ environment variable
      3. /etc/timezone file (Debian/Ubuntu)
      4. tzlocal package if installed (optional)
    """
    # 1. /etc/localtime symlink
    try:
        link = os.readlink("/etc/localtime")
        for marker in ("zoneinfo/", "zoneinfo.default/"):
            if marker in link:
                tz = link.split(marker, 1)[1]
                if tz:
                    return tz
    except (OSError, AttributeError):
        pass
    # 2. TZ env var
    tz = os.environ.get("TZ", "").strip()
    if tz and "/" in tz:
        return tz
    # 3. /etc/timezone (one-line file)
    try:
        with open("/etc/timezone") as f:
            tz = f.read().strip()
            if tz:
                return tz
    except OSError:
        pass
    # 4. tzlocal package (best, but optional)
    try:
        import tzlocal  # type: ignore
        return str(tzlocal.get_localzone())
    except Exception:
        pass
    return "UTC"


# ---------------------------------------------------------------------------
# Bridge-driven auto-detect
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DetectedAccount:
    """Pre-filled record we hand to the add-account form."""
    login: int
    alias: str
    broker: str
    type: str
    user_tz: str
    risk_baseline_equity: float
    server: str
    company: str
    name: str
    currency: str
    leverage: int
    balance: float
    equity: float
    trade_mode_label: str

    @property
    def is_valid(self) -> bool:
        return self.login >= 10000  # real MT5 logins are ≥ 5 digits


def detect_account_via_bridge(
    bridge: MT5AccountClient,
    *, force_refresh: bool = True,
) -> Optional[DetectedAccount]:
    """Probe the running bridge and return a DetectedAccount, or None on
    failure. Errors raised by the bridge are caught and logged via the
    return-None path — callers should handle the None case (offline bridge,
    no connected terminal)."""
    try:
        ai = bridge.account_info(force_refresh=force_refresh)
    except Exception:
        return None
    if ai.login <= 0:
        return None
    broker = infer_broker_from_server(ai.server, company=ai.company)
    acct_type = infer_type_from_account_info(ai, broker)
    user_tz = detect_local_tz()
    # Guess a sensible alias: "<Broker> <size>k Challenge" or "<Broker> Live"
    if acct_type.startswith(("challenge_", "verification_", "funded_")):
        alias = f"{broker} {acct_type.replace('_', ' ').title()}"
    else:
        alias = f"{broker} {ai.trade_mode_label.title()}"
    # Use balance as the inferred risk baseline (matches FTMO start equity
    # for a fresh challenge; for already-failed accounts the user can
    # override in the form before submit).
    baseline = ai.balance if ai.balance > 0 else ai.equity
    return DetectedAccount(
        login=int(ai.login),
        alias=alias,
        broker=broker,
        type=acct_type,
        user_tz=user_tz,
        risk_baseline_equity=float(baseline),
        server=ai.server,
        company=ai.company,
        name=ai.name,
        currency=ai.currency,
        leverage=int(ai.leverage),
        balance=float(ai.balance),
        equity=float(ai.equity),
        trade_mode_label=ai.trade_mode_label,
    )
