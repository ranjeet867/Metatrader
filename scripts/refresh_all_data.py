#!/usr/bin/env python3
"""
refresh_all_data.py — refetch every cached parquet from the MT5 file bridge.

For each existing data/<ticker>_<tf>.parquet, request a fresh batch of bars
from the MT5 EA via core.data.fetch_from_bridge, validate (validate_candles
runs inside save_parquet), and overwrite — only if the fetch succeeded.

Failure modes that are SKIPPED (not raised):
  - bridge timeout (MT5 EA not running)
  - bridge returned 0 candles (broker doesn't recognise that symbol/tf today)
  - any other broker-side error

Failure modes that ARE raised (we want to know about these):
  - validation failure on returned data (bad candles)
  - file system errors writing the parquet

Per-file report:
  ticker | tf | bars_before → bars_after | last_ts_before → last_ts_after | status

Also fetches a few "wishlist" cells that aren't yet cached, so this is the
single command to bring the data dir up to date.

Usage:
  python scripts/refresh_all_data.py
  python scripts/refresh_all_data.py --bars-d1 2000 --bars-h1 8000 --bars-m15 8000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from core.data import fetch_from_bridge, load_parquet, save_parquet


# Always make sure these are present in addition to whatever's already cached.
WISHLIST_CELLS: list[tuple[str, str]] = [
    ("US100.cash", "D1"),
    ("US500.cash", "D1"),
    ("GER40.cash", "D1"),
    ("EU50.cash",  "D1"),
    # Phase 2 stocks (US single-names) + FX cross pairs.
    # Each fetched at all 3 TFs (M15, H1, D1) so the optimizer can sweep
    # them automatically.
    *[(sym, tf) for sym in
      ("AMD", "AMZN", "AVGO", "CSCO", "INTC", "MSFT", "NVDA",
       "EURGBP", "EURJPY", "USDCNH", "USDSEK")
      for tf in ("M15", "H1", "D1")],
    # Crypto — added 2026-05-05 after user confirmed BTCUSD is live in
    # FTMO Market Watch (bid 80078.20 / ask 80079.20). H4 included
    # because crypto trades 24/7 — H4 is a popular crypto cell whereas
    # FX/index H4 was deemed redundant against H1 + D1.
    *[("BTCUSD", tf) for tf in ("M15", "H1", "H4", "D1")],
]


def parse_filename(p: Path) -> tuple[str, str] | None:
    """data/US100.cash_H1.parquet -> ('US100.cash', 'H1'). Last underscore splits."""
    stem = p.stem
    if "_" not in stem:
        return None
    ticker, tf = stem.rsplit("_", 1)
    return ticker, tf


def existing_pairs() -> list[tuple[str, str]]:
    pairs = []
    for path in sorted((ROOT / "data").glob("*.parquet")):
        parsed = parse_filename(path)
        if parsed is not None:
            pairs.append(parsed)
    return pairs


def bars_for_tf(tf: str, bars_d1: int, bars_h1: int, bars_m15: int) -> int:
    return {"D1": bars_d1, "H1": bars_h1, "H4": bars_h1, "M15": bars_m15}.get(tf, 8000)


def refresh_one(ticker: str, tf: str, n_bars: int) -> dict:
    """Try to refresh a single (ticker, tf). Return a result dict (never raises
    bridge errors — those become 'skipped' status)."""
    out_path = ROOT / "data" / f"{ticker}_{tf}.parquet"

    bars_before = 0
    last_before: pd.Timestamp | None = None
    if out_path.exists():
        try:
            old = load_parquet(out_path)
            bars_before = len(old)
            last_before = old["time"].iloc[-1]
        except Exception as e:
            return {"ticker": ticker, "tf": tf, "status": f"err loading existing: {e}"}

    try:
        fresh = fetch_from_bridge(ticker, tf, n_bars)
    except (TimeoutError, RuntimeError) as e:
        return {
            "ticker": ticker, "tf": tf,
            "bars_before": bars_before,
            "last_before": last_before,
            "status": f"skipped ({type(e).__name__}: {e})",
        }

    bars_after = len(fresh)
    last_after = fresh["time"].iloc[-1]
    save_parquet(fresh, out_path)
    if last_before is not None and last_after == last_before:
        status = "no-new-bars"
    elif last_before is None:
        status = "fetched-new"
    else:
        status = f"+{bars_after - bars_before} bars" if bars_after >= bars_before else "rewrote"
    return {
        "ticker": ticker, "tf": tf,
        "bars_before": bars_before, "bars_after": bars_after,
        "last_before": last_before, "last_after": last_after,
        "status": status,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars-d1", type=int, default=2000,
                    help="bars to request for D1 (max history; ~8 years)")
    ap.add_argument("--bars-h1", type=int, default=8000)
    ap.add_argument("--bars-m15", type=int, default=8000)
    ap.add_argument("--include-wishlist", action="store_true", default=True,
                    help="also fetch the WISHLIST_CELLS even if not yet cached")
    args = ap.parse_args()

    targets = list(dict.fromkeys(existing_pairs() + (
        WISHLIST_CELLS if args.include_wishlist else []
    )))
    print(f"\n  Refreshing {len(targets)} (ticker, tf) cells from MT5 bridge...\n")

    rows = []
    for ticker, tf in targets:
        n = bars_for_tf(tf, args.bars_d1, args.bars_h1, args.bars_m15)
        res = refresh_one(ticker, tf, n)
        rows.append(res)
        before = res.get("bars_before", 0)
        after = res.get("bars_after", before)
        lb = res.get("last_before")
        la = res.get("last_after")
        lb_s = "(none)" if lb is None else str(lb)
        la_s = "(skip)" if la is None else str(la)
        print(f"    {ticker:14s} {tf:>4s}   "
              f"{before:>5d} → {after:>5d} bars   "
              f"{lb_s:30s} → {la_s:30s}   "
              f"[{res['status']}]")

    # Summary
    new_or_grown = sum(1 for r in rows
                        if "skipped" not in r["status"]
                        and r["status"] != "no-new-bars")
    skipped = sum(1 for r in rows if r["status"].startswith("skipped"))
    nochange = sum(1 for r in rows if r["status"] == "no-new-bars")
    print(f"\n  Summary: {new_or_grown} updated, {nochange} unchanged, "
          f"{skipped} skipped (bridge unavailable for that symbol).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
