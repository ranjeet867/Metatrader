"""
config.py — read-only loader for data/risk_config.json.

Single source of risk + time-guard configuration. The dashboard's
Account & Risk page is the only writer; everything else (backtest,
paper, live, asset_class, time_guards) READS from here.

If the file is missing, we create it with DEFAULT_CONFIG. We never
fall back silently to defaults — INVARIANT-5: the file's presence is
explicit.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPO_ROOT / "data" / "risk_config.json"


# Defaults baked into the codebase. The on-disk file is initialised from this
# the first time it's needed; thereafter the file IS the source of truth.
DEFAULT_CONFIG: dict[str, Any] = {
    "weekend_flat_all": True,
    "daily_close_flat_classes": ["stock", "index"],
    "us_session_close_utc": "20:00",
    "flat_buffer_minutes": 5,
    "no_entry_minutes_before_close": 30,
    "daily_loss_cap_pct": 4.5,
    "max_consecutive_losses": 3,
    "max_open_positions": 3,
    "ftmo_daily_reset_utc": "22:00",
    "asset_class_overrides": {
        "stock":  ["META", "NVDA", "AAPL", "TSLA", "MSFT", "AMZN", "GOOGL"],
        "index":  ["US100.cash", "US500.cash", "GER40.cash", "EU50.cash",
                    "US30", "NAS100", "SPX500", "JP225"],
        "metal":  ["XAUUSD", "XAGUSD", "XPTUSD"],
        "energy": ["USOIL", "UKOIL", "NGAS"],
        "fx":     [],
    },
    "live_safety": {
        "allowed_accounts": [],
        "override_parity_recency": False,
    },
}


KNOWN_TOP_LEVEL_KEYS = set(DEFAULT_CONFIG.keys())


@dataclass(frozen=True)
class RiskConfig:
    """Typed view of risk_config.json. Pass-through dict for unknown keys."""
    weekend_flat_all: bool
    daily_close_flat_classes: tuple[str, ...]
    us_session_close_utc: str           # "HH:MM"
    flat_buffer_minutes: int
    no_entry_minutes_before_close: int
    daily_loss_cap_pct: float
    max_consecutive_losses: int
    max_open_positions: int
    ftmo_daily_reset_utc: str           # "HH:MM"
    asset_class_overrides: dict[str, tuple[str, ...]]
    live_safety_allowed_accounts: tuple[int, ...]
    live_safety_override_parity_recency: bool
    raw: dict[str, Any] = field(default_factory=dict, compare=False)


def _ensure_file(path: Path) -> None:
    """Create the config file with DEFAULT_CONFIG if missing. Idempotent."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
                     encoding="utf-8")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> RiskConfig:
    """Load + validate. Creates the file with defaults on first call.

    Validation rules:
      * Required top-level keys present (otherwise raise ValueError).
      * Types coerce-or-fail, no silent defaults for required values.
      * Unknown extra keys are KEPT in `raw` and warned (caller decides what
        to do with the warning).

    Returns a frozen RiskConfig.
    """
    path = Path(path)
    _ensure_file(path)
    body = path.read_text(encoding="utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"risk_config.json is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError(f"risk_config.json must be a JSON object; got {type(data).__name__}")

    missing = KNOWN_TOP_LEVEL_KEYS - data.keys()
    if missing:
        raise ValueError(
            f"risk_config.json is missing required keys: {sorted(missing)}. "
            "Delete the file to regenerate defaults, or edit it on Page 5."
        )

    aco = data["asset_class_overrides"]
    if not isinstance(aco, dict):
        raise ValueError("asset_class_overrides must be an object")
    aco_norm = {k: tuple(v) for k, v in aco.items()}

    safety = data.get("live_safety", {})
    if not isinstance(safety, dict):
        raise ValueError("live_safety must be an object")

    return RiskConfig(
        weekend_flat_all=bool(data["weekend_flat_all"]),
        daily_close_flat_classes=tuple(data["daily_close_flat_classes"]),
        us_session_close_utc=str(data["us_session_close_utc"]),
        flat_buffer_minutes=int(data["flat_buffer_minutes"]),
        no_entry_minutes_before_close=int(data["no_entry_minutes_before_close"]),
        daily_loss_cap_pct=float(data["daily_loss_cap_pct"]),
        max_consecutive_losses=int(data["max_consecutive_losses"]),
        max_open_positions=int(data["max_open_positions"]),
        ftmo_daily_reset_utc=str(data["ftmo_daily_reset_utc"]),
        asset_class_overrides=aco_norm,
        live_safety_allowed_accounts=tuple(safety.get("allowed_accounts", [])),
        live_safety_override_parity_recency=bool(
            safety.get("override_parity_recency", False)
        ),
        raw=copy.deepcopy(data),
    )


def save_config(cfg_dict: dict[str, Any],
                path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist a config dict (mostly via the dashboard). Validates by
    round-tripping through load_config to catch bad values early."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg_dict, indent=2) + "\n", encoding="utf-8")
    # Validate by loading from the tmp file
    load_config(tmp)
    tmp.replace(path)


def default_config_dict() -> dict[str, Any]:
    """Return a fresh deep copy of the defaults — for the UI's reset button."""
    return copy.deepcopy(DEFAULT_CONFIG)
