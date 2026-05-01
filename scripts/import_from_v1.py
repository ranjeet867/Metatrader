#!/usr/bin/env python3
"""
import_from_v1.py — copy a candle parquet from v1's cache into v2/data/,
re-validating it through v2's strict validator.

If v1 already has the data we need (very likely for US100.cash H1), this is
the fastest path to start backtesting in v2 — no broker round-trip needed.

Searches v1's standard cache locations in this order:
  1. data/candles_cache/<ticker>_<tf>.parquet
  2. data/backtests/_daily_<date>/candles/<ticker>_<tf>.parquet  (newest)
  3. data/backtests/_cache_<stamp>/candles/<ticker>_<tf>.parquet (newest)

Then validates via core.data.save_parquet (which calls validate_candles)
before writing to v2's data/<ticker>_<tf>.parquet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V1_ROOT = Path.home() / "Documents" / "mt5_quant_trader"
sys.path.insert(0, str(ROOT))

import pandas as pd

from core.data import save_parquet
from core.storage import validate_candles


def find_v1_parquet(ticker: str, tf: str) -> Path | None:
    """Look in standard v1 cache locations, newest first."""
    candidates = []

    direct = V1_ROOT / "data" / "candles_cache" / f"{ticker}_{tf}.parquet"
    if direct.exists():
        candidates.append(direct)

    bt_dir = V1_ROOT / "data" / "backtests"
    if bt_dir.exists():
        for sub in sorted(bt_dir.iterdir(), reverse=True):
            if not sub.is_dir():
                continue
            cdir = sub / "candles"
            if not cdir.exists():
                continue
            p = cdir / f"{ticker}_{tf}.parquet"
            if p.exists():
                candidates.append(p)

    if not candidates:
        return None
    # Pick the LARGEST file — usually the most complete history
    return max(candidates, key=lambda p: p.stat().st_size)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="US100.cash")
    ap.add_argument("--tf", default="H1")
    args = ap.parse_args()

    src = find_v1_parquet(args.ticker, args.tf)
    if src is None:
        print(f"ERROR: no v1 parquet found for {args.ticker} {args.tf}")
        print(f"       searched under {V1_ROOT}/data/")
        return 2

    print(f"Found v1 parquet: {src}")
    print(f"  size: {src.stat().st_size / 1024:.1f} KB")

    df = pd.read_parquet(src)
    print(f"  loaded: {len(df)} bars")

    # Normalize column names if v1 used different ones
    rename = {"o": "open", "h": "high", "l": "low", "c": "close",
              "v": "volume", "tick_volume": "volume"}
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    # Ensure tz-aware UTC time
    if "time" in df.columns:
        if not pd.api.types.is_datetime64_any_dtype(df["time"]):
            df["time"] = pd.to_datetime(df["time"], utc=True)
        if df["time"].dt.tz is None:
            df["time"] = df["time"].dt.tz_localize("UTC")
        elif df["time"].dt.tz.utcoffset(None) != __import__("datetime").timedelta(0):
            df["time"] = df["time"].dt.tz_convert("UTC")

    # Drop duplicates, sort by time
    n_before = len(df)
    df = df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
    if len(df) < n_before:
        print(f"  removed {n_before - len(df)} duplicate timestamps")

    # Keep only the columns we need
    needed = ["time", "open", "high", "low", "close", "volume"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        print(f"ERROR: missing columns {missing}; have {list(df.columns)}")
        return 3
    df = df[needed]

    # Validate (will raise on bad data — caught here for friendly message)
    try:
        validate_candles(df)
    except ValueError as e:
        print(f"ERROR: v1 parquet failed v2 validation: {e}")
        return 4

    # Save to v2/data/
    out = ROOT / "data" / f"{args.ticker}_{args.tf}.parquet"
    save_parquet(df, out)
    print(f"  ✓ Saved → {out}")
    print(f"  ✓ {len(df)} bars   first={df['time'].iloc[0]}   last={df['time'].iloc[-1]}")
    print(f"\nReady to backtest:  make backtest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
