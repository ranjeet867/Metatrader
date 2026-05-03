"""
state.py — central place for session-state keys + cross-page shared state.

Streamlit session_state lives only inside one browser session. We use
Streamlit's @st.cache_resource for objects that must survive page reruns
(paper_loop instance, MT5AccountClient, RiskTracker, etc.).
"""
from __future__ import annotations

import dataclasses
import importlib
import inspect
import sys
from pathlib import Path
from typing import Any

import streamlit as st


REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Strategy + data discovery (re-used across every page)
# ---------------------------------------------------------------------------

@st.cache_resource
def discover_strategies() -> dict[str, tuple[type, type | None]]:
    """{strategy_name: (StrategyClass, ParamsClass)} — same logic as control.py."""
    out: dict[str, tuple[type, type | None]] = {}
    strat_dir = REPO / "strategies"
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    for path in sorted(strat_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        modname = f"strategies.{path.stem}"
        try:
            mod = importlib.import_module(modname)
        except Exception:
            continue
        strat_cls: type | None = None
        params_cls: type | None = None
        for clsname, cls in inspect.getmembers(mod, inspect.isclass):
            if cls.__module__ != mod.__name__:
                continue
            if dataclasses.is_dataclass(cls) and clsname.endswith("Params"):
                params_cls = cls
            elif (hasattr(cls, "name") and isinstance(cls.name, str)
                  and hasattr(cls, "signals")
                  and not dataclasses.is_dataclass(cls)):
                strat_cls = cls
        if strat_cls is not None:
            out[strat_cls.name] = (strat_cls, params_cls)
    return out


def discover_data() -> dict[str, dict[str, Path]]:
    out: dict[str, dict[str, Path]] = {}
    data_dir = REPO / "data"
    for path in sorted(data_dir.glob("*.parquet")):
        stem = path.stem
        if "_" not in stem:
            continue
        ticker, tf = stem.rsplit("_", 1)
        out.setdefault(ticker, {})[tf] = path
    return out


# Per-ticker money_per_unit_USD_per_lot defaults (mirrors scripts/sweep_grid.py)
DEFAULT_MONEY_PER_UNIT: dict[str, float] = {
    "US100.cash": 1.0, "US500.cash": 1.0, "GER40.cash": 1.0,
    "EU50.cash":  1.10,
    "EURUSD": 100_000.0, "GBPUSD": 100_000.0, "AUDUSD": 100_000.0,
    "NZDUSD": 100_000.0, "USDJPY": 700.0, "GBPJPY": 700.0,
}
DEFAULT_LOTS: dict[str, float] = {
    "US100.cash": 6.5, "US500.cash": 20.0, "GER40.cash": 3.0,
    "EU50.cash": 20.0,
    "EURUSD": 1.0, "GBPUSD": 1.0, "AUDUSD": 1.5, "NZDUSD": 1.5,
    "USDJPY": 1.0, "GBPJPY": 0.7,
}


# ---------------------------------------------------------------------------
# Session-state keys
# ---------------------------------------------------------------------------

KEY_PROMOTE_TO_STUDIO = "_promote_to_studio"
KEY_PROMOTE_TO_PAPER = "_promote_to_paper"
KEY_LAST_BACKTEST = "_last_backtest"
KEY_LAST_BACKTEST_META = "_last_backtest_meta"
KEY_FTMO_RISK_ACCEPTED = "_ftmo_risk_accepted"
KEY_OVERRIDE_PARITY_RECENCY = "_override_parity_recency"
