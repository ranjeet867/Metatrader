"""
symbol_info_loader.py — read data/symbol_info.json into SymbolInfo objects.

The dashboard / backtest / replay all need static SymbolInfo at run time
(the live bridge isn't queried per-bar). This module is the read path;
scripts/refresh_symbol_info.py is the write path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from core.position_sizer import SymbolInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "data" / "symbol_info.json"


REQUIRED_FIELDS = {
    "tick_size", "tick_value", "volume_step", "volume_min", "volume_max",
    "digits", "contract_size",
}


def _validate_one(name: str, d: dict) -> None:
    missing = REQUIRED_FIELDS - d.keys()
    if missing:
        raise ValueError(f"{name}: missing fields {sorted(missing)}")
    if d["tick_size"] <= 0:
        raise ValueError(f"{name}: tick_size must be > 0")
    if d["tick_value"] <= 0:
        raise ValueError(f"{name}: tick_value must be > 0")
    if d["volume_step"] <= 0 or d["volume_min"] <= 0:
        raise ValueError(f"{name}: volume_step / volume_min must be > 0")
    if d["volume_max"] < d["volume_min"]:
        raise ValueError(f"{name}: volume_max < volume_min")


def load_all(path: str | Path = DEFAULT_PATH) -> dict[str, SymbolInfo]:
    """Read the full symbol_info.json into {name: SymbolInfo}.

    Validates each entry on the way through; raises ValueError on bad data.
    INVARIANT-5: never silently default — the file must be valid.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"{p} not found. Run scripts/refresh_symbol_info.py to create it, "
            "or check it in from a sibling environment."
        )
    body = p.read_text(encoding="utf-8")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"{p} is not valid JSON: {e}") from e
    syms = data.get("symbols", {})
    if not isinstance(syms, dict):
        raise ValueError(f"{p}: 'symbols' must be an object")
    out: dict[str, SymbolInfo] = {}
    for name, d in syms.items():
        if name.startswith("_"):
            continue
        _validate_one(name, d)
        out[name] = SymbolInfo(
            name=name,
            tick_size=float(d["tick_size"]),
            tick_value=float(d["tick_value"]),
            volume_step=float(d["volume_step"]),
            volume_min=float(d["volume_min"]),
            volume_max=float(d["volume_max"]),
            digits=int(d["digits"]),
            contract_size=float(d["contract_size"]),
        )
    return out


def load(name: str, path: str | Path = DEFAULT_PATH) -> SymbolInfo:
    """Load a single symbol's metadata. Raises KeyError if missing."""
    all_syms = load_all(path)
    if name not in all_syms:
        raise KeyError(f"symbol {name!r} not in {path}; "
                        f"available: {sorted(all_syms.keys())}")
    return all_syms[name]


def try_load(name: str, path: str | Path = DEFAULT_PATH) -> Optional[SymbolInfo]:
    """Same as load() but returns None instead of raising. For UI paths
    that want to gracefully degrade."""
    try:
        return load(name, path)
    except (FileNotFoundError, KeyError, ValueError):
        return None
