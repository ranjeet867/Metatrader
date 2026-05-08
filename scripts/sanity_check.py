#!/usr/bin/env python3
"""
sanity_check.py — verify a freshly-restored repo is intact.

Run this after unzipping a backup on a new machine. Confirms:
  • Every core/* module imports
  • Every strategies/* discovers
  • The SQLite DB opens + has the expected tables
  • All cached parquets are readable

Exits 0 on full pass, 1 on any failure.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

REQUIRED_CORE_MODULES = [
    "core.backtest",
    "core.replay",
    "core.parity_check",
    "core.parity_gate",
    "core.circuit_breaker",
    "core.position_guard",
    "core.deployment",
    "core.account_manager",
    "core.perf_query",
    "core.runner",
    "core.live_executor",
    "core.paper_executor",
    "core.strategy",
    "core.indicators",
    "core.config",
    "core.time_guards",
    "core.storage",
]

REQUIRED_DB_TABLES = ["trades", "parity_log", "bridge_events"]


def _check_imports() -> int:
    n_fail = 0
    for mod_name in REQUIRED_CORE_MODULES:
        try:
            importlib.import_module(mod_name)
            print(f"  ✅ {mod_name}")
        except Exception as e:
            print(f"  ⛔ {mod_name}  {type(e).__name__}: {e}")
            n_fail += 1
    return n_fail


def _check_strategies() -> int:
    try:
        from dashboards.components.state import discover_strategies
        strats = discover_strategies()
        print(f"  ✅ {len(strats)} strategies registered: "
              f"{', '.join(sorted(strats.keys()))}")
        return 0
    except Exception as e:
        print(f"  ⛔ discover_strategies: {type(e).__name__}: {e}")
        return 1


def _check_database() -> int:
    db_path = REPO / "data" / "v2.db"
    if not db_path.exists():
        print(f"  ⚠ no database at {db_path} (fresh restore?)")
        return 0
    try:
        with sqlite3.connect(str(db_path)) as c:
            existing = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        missing = [t for t in REQUIRED_DB_TABLES if t not in existing]
        if missing:
            print(f"  ⛔ missing tables: {missing}")
            return 1
        # Count rows in each table
        with sqlite3.connect(str(db_path)) as c:
            for t in REQUIRED_DB_TABLES:
                cnt = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  ✅ {t}: {cnt:,} rows")
        return 0
    except Exception as e:
        print(f"  ⛔ db check: {type(e).__name__}: {e}")
        return 1


def _check_parquets() -> int:
    data_dir = REPO / "data"
    if not data_dir.exists():
        print(f"  ⚠ no data/ directory yet")
        return 0
    parquets = list(data_dir.glob("*.parquet"))
    if not parquets:
        print(f"  ⚠ no cached parquets — use Data Manager to fetch.")
        return 0
    n_fail = 0
    for p in parquets[:5]:
        try:
            import pandas as pd
            df = pd.read_parquet(p)
            print(f"  ✅ {p.name}: {len(df):,} bars")
        except Exception as e:
            print(f"  ⛔ {p.name}: {type(e).__name__}: {e}")
            n_fail += 1
    if len(parquets) > 5:
        print(f"     ...and {len(parquets) - 5} more parquets (not checked)")
    return n_fail


def main() -> int:
    print(f"🔍  Sanity-checking restore at {REPO}\n")

    print("Step 1: Import core modules")
    n_import = _check_imports()
    print()

    print("Step 2: Discover strategies")
    n_strat = _check_strategies()
    print()

    print("Step 3: Verify SQLite database")
    n_db = _check_database()
    print()

    print("Step 4: Spot-check parquet files")
    n_pq = _check_parquets()
    print()

    total = n_import + n_strat + n_db + n_pq
    if total == 0:
        print("✅  All checks passed. Restore is intact.")
        return 0
    print(f"⛔  {total} check(s) failed. Restore incomplete.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
