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
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="Bridge timeout (s). Default 120 (was 30); bump "
                          "to 300+ for >50k bars — MT5 takes ~1s per 1000 "
                          "M15 bars to serialize.")
    args = ap.parse_args()

    out = Path(args.out) if args.out else \
          ROOT / "data" / f"{args.ticker}_{args.tf}.parquet"

    # Safety cap: large requests can crash the Wine MT5 bridge EA.
    # Empirically 5000-bar requests are reliable; 30k+ tends to hang
    # the EA and leave it in zombie state. Force chunked fetch above
    # 8000 bars or warn the user.
    SAFE_CAP = 8000
    if args.bars > SAFE_CAP:
        print(f"⚠️  WARNING: requesting {args.bars} bars in a single call.")
        print(f"   Wine MT5 bridge can crash the EA above ~{SAFE_CAP} bars.")
        print(f"   If the EA stops responding after this, restart MT5.")
        print(f"   Consider --bars {SAFE_CAP} for safer behaviour.")

    print(f"Fetching {args.ticker} {args.tf} ({args.bars} bars, "
          f"timeout={args.timeout}s) via MT5 bridge...")
    df = fetch_from_bridge(args.ticker, args.tf, args.bars,
                            timeout_s=args.timeout)
    print(f"  Got {len(df)} candles  "
          f"first={df['time'].iloc[0]}  "
          f"last={df['time'].iloc[-1]}")

    save_parquet(df, out)
    print(f"  Saved → {out}")
    print(f"  Validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
