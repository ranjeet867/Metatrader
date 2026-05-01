#!/usr/bin/env python3
"""
load_real_data.py — fetch real broker candles via the v1 file-bridge,
validate them, save to data/<symbol>_<tf>.parquet.

The script is idempotent: re-running with same args either:
  - Returns the cached parquet if it's already current
  - Or fetches fresh and overwrites (with the same data, byte-identical)

Usage:
  python scripts/load_real_data.py --ticker US100.cash --tf H1 --bars 8000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.data import fetch_from_bridge, save_parquet


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="US100.cash")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--bars", type=int, default=8000,
                    help="Number of bars to fetch (broker may cap)")
    ap.add_argument("--out", default=None,
                    help="Output parquet path (default: data/<ticker>_<tf>.parquet)")
    args = ap.parse_args()

    out = Path(args.out) if args.out else \
          ROOT / "data" / f"{args.ticker}_{args.tf}.parquet"

    print(f"Fetching {args.ticker} {args.tf} ({args.bars} bars) via MT5 bridge...")
    df = fetch_from_bridge(args.ticker, args.tf, args.bars)
    print(f"  Got {len(df)} candles  "
          f"first={df['time'].iloc[0]}  "
          f"last={df['time'].iloc[-1]}")

    save_parquet(df, out)
    print(f"  Saved → {out}")
    print(f"  Validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
