#!/usr/bin/env python3
"""
refresh_symbol_info.py — query the MT5 bridge for each cached symbol's
metadata and write data/symbol_info.json.

Usage:
    python scripts/refresh_symbol_info.py
    python scripts/refresh_symbol_info.py --symbol US100.cash
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.mt5_account import MT5AccountClient   # noqa: E402


SYMBOL_INFO_PATH = REPO / "data" / "symbol_info.json"


def _discover_symbols() -> list[str]:
    out = set()
    for p in (REPO / "data").glob("*.parquet"):
        stem = p.stem
        if "_" not in stem:
            continue
        ticker, _tf = stem.rsplit("_", 1)
        out.add(ticker)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None,
                     help="refresh only this symbol (default: all in data/)")
    ap.add_argument("--bridge-timeout-s", type=float, default=10.0)
    args = ap.parse_args()

    symbols = [args.symbol] if args.symbol else _discover_symbols()
    if not symbols:
        raise SystemExit("no symbols to refresh (no parquets in data/)")

    # Load existing JSON so we update incrementally
    if SYMBOL_INFO_PATH.exists():
        existing = json.loads(SYMBOL_INFO_PATH.read_text())
    else:
        existing = {"_meta": {"schema_version": 1}, "symbols": {}}

    if "symbols" not in existing:
        existing["symbols"] = {}

    client = MT5AccountClient()
    updated = 0
    for sym in symbols:
        try:
            si = client.symbol_info(sym, force_refresh=True)
        except Exception as e:
            print(f"  [skip] {sym}: bridge error: {e}")
            continue
        existing["symbols"][sym] = {
            "tick_size": si.tick_size,
            "tick_value": si.tick_value,
            "volume_step": si.volume_step,
            "volume_min": si.volume_min,
            "volume_max": getattr(si, "volume_max", 100.0),
            "digits": getattr(si, "digits", 2),
            "contract_size": si.contract_size,
        }
        print(f"  [ok]   {sym}: tick_size={si.tick_size}  "
              f"tick_value={si.tick_value}  step={si.volume_step}")
        updated += 1

    existing["_meta"]["last_refreshed_utc"] = datetime.now(timezone.utc).isoformat()
    SYMBOL_INFO_PATH.write_text(json.dumps(existing, indent=2) + "\n",
                                  encoding="utf-8")
    print(f"\nWrote {SYMBOL_INFO_PATH.relative_to(REPO)} "
          f"({updated} symbols updated)")


if __name__ == "__main__":
    main()
