#!/usr/bin/env python3
"""
inspect_symbol.py — query MT5 bridge for a symbol's tick metadata so we can
set money_per_unit_price correctly in the backtester.

Reuses v1's mt5_client by importing from v1 as a package.

Usage:
  python scripts/inspect_symbol.py --ticker US100.cash
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

V1_ROOT = Path.home() / "Documents" / "mt5_quant_trader"
# Insert v1 ROOT (not v1/src) so `from src.mt5_client import ...` resolves
# the package correctly with its relative `from .utils import ...`.
sys.path.insert(0, str(V1_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="US100.cash")
    args = ap.parse_args()

    from src.config import load_config
    from src.mt5_client import build_client

    cfg = load_config(V1_ROOT / "config.yaml")
    client = build_client(cfg)
    client.connect()
    info = client.symbol_info(args.ticker)

    if info is None:
        print(f"ERROR: symbol_info({args.ticker}) returned None")
        return 2

    print(f"\nSymbol metadata for {args.ticker}:")
    print(f"  tick_size       (trade_tick_size)  = {info.trade_tick_size}")
    print(f"  tick_value      (trade_tick_value) = {info.trade_tick_value}")
    print(f"  volume_step     = {info.volume_step}")
    print(f"  volume_min      = {info.volume_min}")
    print(f"  volume_max      = {getattr(info, 'volume_max', '?')}")
    print(f"  digits          = {getattr(info, 'digits', '?')}")
    print(f"  point           = {getattr(info, 'point', '?')}")
    print(f"  spread          = {getattr(info, 'spread', '?')}")
    print(f"  contract_size   = {getattr(info, 'trade_contract_size', '?')}")

    if info.trade_tick_size and info.trade_tick_size > 0:
        money_per_unit = info.trade_tick_value / info.trade_tick_size
        print(f"\n  money_per_unit_price = tick_value / tick_size = {money_per_unit}")
        print(f"\nUse this when running backtest:")
        print(f"  .venv/bin/python scripts/run_backtest.py --money-per-unit {money_per_unit}")

    client.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
