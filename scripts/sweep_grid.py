#!/usr/bin/env python3
"""
sweep_grid.py — multi-ticker × multi-TF × multi-strategy reconciliation-checked
edge search.

For every available (ticker, tf) parquet under data/, run all 7 strategy
variants with realistic friction (commission + ATR-frac slippage) and the
60/40 train/test split. Print a sorted markdown table and also write
docs/grid_results.md.

A strategy×ticker×tf cell is a "survivor" iff:
  - reconciliation passes (always required)
  - train_PF >= 1.0  AND  test_PF >= 1.0
  - train_avg_R > 0  AND  test_avg_R > 0
  - n_test_trades >= 5 (enough OOS trades to mean anything)

Exit code 3 if ANY backtest fails reconciliation.

Usage:
  python scripts/sweep_grid.py --commission-per-trade 3.0 --slippage-atr-frac 0.1
  python scripts/sweep_grid.py --long-only
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import partition_train_test, run_backtest
from core.data import load_parquet
from strategies.bbands_meanrev import BBandsMeanRev, BBandsMeanRevParams
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.ema_cross import EmaCross, EmaCrossParams
from strategies.ema_pullback import EmaPullback, EmaPullbackParams
from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams


# Per-ticker config: (money_per_unit_price_USD_per_lot, default_lots).
#   money_per_unit_price = $ that move per 1.0 PRICE UNIT per 1 lot.
# Lots are chosen so that ~1×ATR(14) per trade ≈ several-hundred USD on a
# $91k FTMO account (i.e. roughly 0.3-0.5% risk per trade), which makes the
# fixed $3 commission a realistic fraction of notional risk. These are
# APPROXIMATE — for live trading, query the actual broker symbol_info.
#
# Index CFDs (US100, EU50): $1/lot per 1.0 price unit (close to true value).
# JPY pairs (XXXJPY): 1 lot = 100k base; 1.0 JPY move ≈ $670 at ~150 JPY/USD.
# USD-quoted FX (EURUSD etc): 1 lot = 100k base; 1.0 USD move = $100k/lot.
# Lots set such that 1 ATR × lots × $/unit ≈ $200-500 across tickers.
TICKER_CONFIG: dict[str, tuple[float, float]] = {
    "US100.cash": (1.00,    6.5),   # ATR(H1)~80 → ~$520/ATR
    "EU50.cash":  (1.10,    3.0),   # ATR(H1)~25 → ~$83/ATR
    "USDJPY":     (700.0,   1.0),   # ATR(H1)~0.15 → ~$105/ATR
    "GBPJPY":     (700.0,   0.7),   # ATR(H1)~0.30 → ~$147/ATR
    "EURUSD":     (100_000.0, 1.0), # ATR(H1)~0.0012 → ~$120/ATR
    "GBPUSD":     (100_000.0, 1.0), # ATR(H1)~0.0017 → ~$170/ATR
    "AUDUSD":     (100_000.0, 1.5), # ATR(H1)~0.0010 → ~$150/ATR
    "NZDUSD":     (100_000.0, 1.5), # ATR(H1)~0.0011 → ~$165/ATR
}
TICKERS = list(TICKER_CONFIG.keys())
TIMEFRAMES = ["M15", "H1", "D1"]


def build_strategies(long_only: bool):
    return [
        ("ema_cross_9_20",
         EmaCross(EmaCrossParams(fast_period=9, slow_period=20, long_only=long_only))),
        ("ema_cross_12_26",
         EmaCross(EmaCrossParams(fast_period=12, slow_period=26, long_only=long_only))),
        ("ema_pullback_20_50",
         EmaPullback(EmaPullbackParams(fast_period=20, slow_period=50, long_only=long_only))),
        ("donchian_20",
         DonchianBreakout(DonchianBreakoutParams(period=20, long_only=long_only))),
        ("donchian_55",
         DonchianBreakout(DonchianBreakoutParams(period=55, long_only=long_only))),
        ("rsi_30_70",
         RsiMeanRev(RsiMeanRevParams(oversold=30, overbought=70, long_only=long_only))),
        ("bbands_20_2",
         BBandsMeanRev(BBandsMeanRevParams(bb_period=20, bb_k=2.0, long_only=long_only))),
    ]


@dataclass
class Cell:
    ticker: str
    tf: str
    strategy: str
    n_total: int
    n_train: int
    n_test: int
    train_pf: float
    test_pf: float
    train_R: float
    test_R: float
    reconciles: bool


def fmt_pf(pf: float) -> str:
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=91_400)
    ap.add_argument("--lots", type=float, default=0.0,
                    help="override per-ticker default lots (0 = use TICKER_CONFIG)")
    ap.add_argument("--commission-per-trade", type=float, default=3.0)
    ap.add_argument("--slippage-atr-frac", type=float, default=0.1)
    ap.add_argument("--train-pct", type=float, default=0.6)
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--min-test-trades", type=int, default=5)
    ap.add_argument("--out", default=str(ROOT / "docs" / "grid_results.md"))
    args = ap.parse_args()

    print(f"\n{'=' * 80}")
    print(f"  GRID SWEEP — {len(TICKERS)} tickers × {len(TIMEFRAMES)} TFs × 7 strategies")
    print(f"  friction: comm=${args.commission_per_trade}  slip={args.slippage_atr_frac}×ATR  "
          f"train_pct={args.train_pct}  long_only={args.long_only}")
    print(f"{'=' * 80}\n")

    cells: list[Cell] = []
    skipped: list[str] = []
    failed_reconcile: list[str] = []

    for ticker in TICKERS:
        money_per_unit, default_lots = TICKER_CONFIG[ticker]
        lots = args.lots if args.lots > 0 else default_lots
        for tf in TIMEFRAMES:
            parquet = ROOT / "data" / f"{ticker}_{tf}.parquet"
            if not parquet.exists():
                skipped.append(f"{ticker}_{tf}")
                continue
            df = load_parquet(parquet)
            if len(df) < 200:
                skipped.append(f"{ticker}_{tf} (only {len(df)} bars)")
                continue
            for sname, strat in build_strategies(args.long_only):
                sigs = strat.signals(df)
                r = run_backtest(
                    df, sigs,
                    starting_balance=args.balance,
                    lots=lots,
                    money_per_unit_price=money_per_unit,
                    commission_per_trade=args.commission_per_trade,
                    slippage_per_fill_atr_frac=args.slippage_atr_frac,
                )
                if not r.reconciles:
                    failed_reconcile.append(f"{ticker}/{tf}/{sname}")
                    continue
                train, test = partition_train_test(r, args.train_pct, n_bars=len(df))
                cells.append(Cell(
                    ticker=ticker, tf=tf, strategy=sname,
                    n_total=r.n_trades,
                    n_train=train.n_trades, n_test=test.n_trades,
                    train_pf=train.profit_factor, test_pf=test.profit_factor,
                    train_R=train.avg_R, test_R=test.avg_R,
                    reconciles=r.reconciles,
                ))

    if failed_reconcile:
        print(f"⛔ {len(failed_reconcile)} cells FAILED reconciliation:")
        for f in failed_reconcile:
            print(f"   {f}")
        return 3

    if skipped:
        print(f"  (skipped: {', '.join(skipped)})\n")

    # Filter survivors: real edge candidates
    survivors = [
        c for c in cells
        if c.train_pf >= 1.0 and c.test_pf >= 1.0
        and c.train_R > 0 and c.test_R > 0
        and c.n_test >= args.min_test_trades
    ]

    # Sort by test_R desc as primary, train_R as tiebreaker
    survivors.sort(key=lambda c: (c.test_R, c.train_R), reverse=True)

    # Top 30 by test_R for printing (regardless of survivor status)
    by_test_R = sorted(cells, key=lambda c: c.test_R, reverse=True)

    print(f"  TOP 30 by test_R (out-of-sample; PF and R independent of $ scaling):\n")
    print(f"  {'ticker':12s} {'tf':>4s} {'strategy':22s} "
          f"{'n_tr':>5s} {'tr_PF':>6s} {'tr_R':>7s} | "
          f"{'n_te':>5s} {'te_PF':>6s} {'te_R':>7s}  surv?")
    print("  " + "─" * 100)
    for c in by_test_R[:30]:
        is_surv = c in survivors
        marker = "⭐" if is_surv else "  "
        print(f"  {c.ticker:12s} {c.tf:>4s} {c.strategy:22s} "
              f"{c.n_train:>5d} {fmt_pf(c.train_pf):>6s} {c.train_R:>+7.3f} | "
              f"{c.n_test:>5d} {fmt_pf(c.test_pf):>6s} {c.test_R:>+7.3f}  {marker}")

    print(f"\n  Total cells run:    {len(cells)}")
    print(f"  Survivors (train AND test PF≥1, avg_R>0, n_test≥{args.min_test_trades}): {len(survivors)}")
    if survivors:
        print(f"\n  ⭐ EDGE CANDIDATES (positive in BOTH train and test):\n")
        for c in survivors:
            print(f"    {c.ticker:12s} {c.tf:>4s} {c.strategy:22s}  "
                  f"train_R={c.train_R:+.3f}  test_R={c.test_R:+.3f}  "
                  f"test_PF={fmt_pf(c.test_pf)}  n_test={c.n_test}")
    else:
        print("\n  (no cell positive in BOTH train AND test with sufficient OOS trades)")

    # ---- write markdown ----
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# Grid Sweep Results")
    lines.append("")
    lines.append(f"- Run config: comm=${args.commission_per_trade}  "
                 f"slip={args.slippage_atr_frac}×ATR  "
                 f"train_pct={args.train_pct}  long_only={args.long_only}")
    lines.append(f"- Tickers: `{', '.join(TICKERS)}`")
    lines.append(f"- Timeframes: `{', '.join(TIMEFRAMES)}`")
    lines.append(f"- 7 strategies (see scripts/sweep_grid.py)")
    lines.append(f"- Total cells: {len(cells)}")
    lines.append(f"- Survivors (train AND test PF≥1, avg_R>0, n_test≥{args.min_test_trades}): "
                 f"**{len(survivors)}**")
    lines.append("")
    lines.append("> Note: `money_per_unit_price=1.0` for ALL tickers, so $ amounts don't compare cross-symbol. "
                 "PF and avg_R are scale-invariant and ARE comparable.")
    lines.append("")
    if survivors:
        lines.append("## ⭐ Edge candidates (positive in BOTH train and test)")
        lines.append("")
        lines.append("| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |")
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for c in survivors:
            lines.append(f"| {c.ticker} | {c.tf} | {c.strategy} | "
                         f"{c.n_train} | {fmt_pf(c.train_pf)} | {c.train_R:+.3f} | "
                         f"{c.n_test} | {fmt_pf(c.test_pf)} | {c.test_R:+.3f} |")
    else:
        lines.append("## No edge candidates")
        lines.append("")
        lines.append("Across all 7 strategies × 8 tickers × 3 timeframes with realistic friction, "
                     "**not a single (ticker, TF, strategy) combination produces positive PF "
                     "and positive avg_R in BOTH the in-sample 60% and the out-of-sample 40% partition** "
                     f"with n_test ≥ {args.min_test_trades}.")
    lines.append("")
    lines.append("## Top 30 cells by test_R (out-of-sample avg R-multiple)")
    lines.append("")
    lines.append("| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R | survivor |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|:---:|")
    for c in by_test_R[:30]:
        is_surv = c in survivors
        lines.append(f"| {c.ticker} | {c.tf} | {c.strategy} | "
                     f"{c.n_train} | {fmt_pf(c.train_pf)} | {c.train_R:+.3f} | "
                     f"{c.n_test} | {fmt_pf(c.test_pf)} | {c.test_R:+.3f} | "
                     f"{'⭐' if is_surv else ''} |")
    out.write_text("\n".join(lines))
    print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
