#!/usr/bin/env python3
"""
sweep_strategies.py — run all 5 strategies on a single (ticker, tf) and
print a comparison table.

Every result is reconciliation-checked. Untrustworthy = halt with exit code 3.

Usage:
  python scripts/sweep_strategies.py
  python scripts/sweep_strategies.py --ticker US100.cash --tf M15 --lots 0.1
  python scripts/sweep_strategies.py --long-only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import partition_train_test, run_backtest
from core.data import load_parquet
from strategies.ema_cross import EmaCross, EmaCrossParams
from strategies.ema_pullback import EmaPullback, EmaPullbackParams
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
from strategies.bbands_meanrev import BBandsMeanRev, BBandsMeanRevParams


def build_strategies(long_only: bool):
    """Each entry: (display name, factory). All use sensible defaults."""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="US100.cash")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--balance", type=float, default=91_400)
    ap.add_argument("--lots", type=float, default=0.1)
    ap.add_argument("--money-per-unit", type=float, default=1.0)
    ap.add_argument("--commission-per-trade", type=float, default=0.0,
                    help="$ deducted per closed trade (round-trip)")
    ap.add_argument("--slippage-atr-frac", type=float, default=0.0,
                    help="per-fill slippage as fraction of ATR(14) at fill bar")
    ap.add_argument("--train-pct", type=float, default=0.6,
                    help="fraction of bars used for IN-SAMPLE (rest is OOS test)")
    ap.add_argument("--long-only", action="store_true")
    args = ap.parse_args()

    parquet = ROOT / "data" / f"{args.ticker}_{args.tf}.parquet"
    if not parquet.exists():
        print(f"ERROR: {parquet} not found. Run `make import-tf TF={args.tf}` first.")
        return 2

    df = load_parquet(parquet)
    print(f"\nLoaded: {parquet.name}   {len(df)} bars   "
          f"first={df['time'].iloc[0]}   last={df['time'].iloc[-1]}")
    print(f"Config: balance=${args.balance:,.0f}  lots={args.lots}  "
          f"money_per_unit={args.money_per_unit}  "
          f"comm=${args.commission_per_trade}/trade  "
          f"slip={args.slippage_atr_frac}×ATR  "
          f"train_pct={args.train_pct}  "
          f"long_only={args.long_only}\n")

    rows = []
    failed_reconcile = []
    for name, strat in build_strategies(args.long_only):
        sigs = strat.signals(df)
        r = run_backtest(df, sigs, starting_balance=args.balance,
                          lots=args.lots,
                          money_per_unit_price=args.money_per_unit,
                          commission_per_trade=args.commission_per_trade,
                          slippage_per_fill_atr_frac=args.slippage_atr_frac)
        if not r.reconciles:
            failed_reconcile.append(name)
        train, test = partition_train_test(r, args.train_pct, n_bars=len(df))
        rec_marker = "✓" if r.reconciles else "⛔"
        rows.append({
            "name": name, "n_sig": len(sigs),
            "n_total": r.n_trades,
            "train_n": train.n_trades, "train_pf": train.profit_factor,
            "train_R": train.avg_R,
            "train_ret": train.sum_pnl / args.balance * 100,
            "test_n": test.n_trades, "test_pf": test.profit_factor,
            "test_R": test.avg_R,
            "test_ret": test.sum_pnl / args.balance * 100,
            "tot_ret": r.equity_curve_pnl / args.balance * 100,
            "sum_pnl": r.sum_realized_pnl,
            "reconcile": rec_marker,
        })

    # ---- print table ----
    print(f"  {'strategy':22s} {'trd':>4s} | "
          f"{'tr_n':>4s} {'tr_PF':>6s} {'tr_R':>6s} {'tr_ret%':>8s} | "
          f"{'te_n':>4s} {'te_PF':>6s} {'te_R':>6s} {'te_ret%':>8s} | "
          f"{'tot_ret%':>8s}  rec")
    print("  " + "─" * 110)
    # Sort by TEST avg_R (out-of-sample is what we trust)
    rows.sort(key=lambda r: -r["test_R"])
    for r in rows:
        # Cap PF for display
        tr_pf = "inf" if r["train_pf"] == float("inf") else f"{r['train_pf']:.2f}"
        te_pf = "inf" if r["test_pf"] == float("inf") else f"{r['test_pf']:.2f}"
        print(f"  {r['name']:22s} {r['n_total']:>4d} | "
              f"{r['train_n']:>4d} {tr_pf:>6s} {r['train_R']:>+6.3f} {r['train_ret']:>+7.2f}% | "
              f"{r['test_n']:>4d} {te_pf:>6s} {r['test_R']:>+6.3f} {r['test_ret']:>+7.2f}% | "
              f"{r['tot_ret']:>+7.2f}%  {r['reconcile']}")
    print()
    # Highlight survivors: train AND test both PF >= 1.0 with positive avg_R
    survivors = [r for r in rows
                 if r["train_pf"] >= 1.0 and r["test_pf"] >= 1.0
                 and r["train_R"] > 0 and r["test_R"] > 0]
    if survivors:
        print("  ⭐ POSITIVE TRAIN+TEST PF survivors (real edge candidates):")
        for r in survivors:
            print(f"     {r['name']}  test_R={r['test_R']:+.3f}  test_ret={r['test_ret']:+.2f}%")
    else:
        print("  (no strategy positive in BOTH train AND test — no edge here)")
    print()

    if failed_reconcile:
        print(f"⛔ {len(failed_reconcile)} strategies FAILED reconciliation: "
              f"{failed_reconcile}")
        print("   Do not trust their numbers. Investigate before continuing.")
        return 3

    print("All strategies reconciled. Numbers above are honest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
