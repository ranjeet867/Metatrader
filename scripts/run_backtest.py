#!/usr/bin/env python3
"""
run_backtest.py — end-to-end EMA cross backtest on a locked parquet.

Reads data/<ticker>_<tf>.parquet, runs EMA cross strategy + reconciliation-
enforced backtester, prints a clean report, asserts reconciliation passes.

Usage:
  python scripts/run_backtest.py
  python scripts/run_backtest.py --ticker US100.cash --tf H1 --balance 91400 --lots 0.5
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import run_backtest
from core.data import load_parquet
from core import storage
from strategies.ema_cross import EmaCross, EmaCrossParams


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="US100.cash")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--balance", type=float, default=91_400)
    ap.add_argument("--lots", type=float, default=0.1)
    ap.add_argument("--money-per-unit", type=float, default=1.0,
                    help="$ per 1.0 price unit per 1 lot (US100.cash default = $1)")
    ap.add_argument("--fast", type=int, default=9)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--stop-atr-mult", type=float, default=1.5)
    ap.add_argument("--target-atr-mult", type=float, default=3.0)
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--db", default=str(ROOT / "data" / "v2.db"))
    args = ap.parse_args()

    parquet_path = ROOT / "data" / f"{args.ticker}_{args.tf}.parquet"
    if not parquet_path.exists():
        print(f"ERROR: {parquet_path} not found. Run scripts/load_real_data.py first.")
        return 2

    print(f"Loading {parquet_path}...")
    df = load_parquet(parquet_path)
    print(f"  {len(df)} bars   "
          f"first={df['time'].iloc[0]}   last={df['time'].iloc[-1]}")

    params = EmaCrossParams(
        fast_period=args.fast, slow_period=args.slow,
        atr_period=14,
        stop_atr_mult=args.stop_atr_mult,
        target_atr_mult=args.target_atr_mult,
        long_only=args.long_only,
    )
    strategy = EmaCross(params)
    signals = strategy.signals(df)
    print(f"\nStrategy: {strategy.name}  params={params}")
    print(f"  Signals generated: {len(signals)}")

    print(f"\nRunning backtest...")
    result = run_backtest(
        df, signals,
        starting_balance=args.balance,
        lots=args.lots,
        money_per_unit_price=args.money_per_unit,
    )

    # ---- Reconciliation gate (HARD FAIL if it doesn't reconcile) ----
    if not result.reconciles:
        print(f"\n⛔ RECONCILIATION FAILED")
        print(f"   sum_realized_pnl = {result.sum_realized_pnl:.6f}")
        print(f"   equity_curve_pnl = {result.equity_curve_pnl:.6f}")
        print(f"   diff             = {result.sum_realized_pnl - result.equity_curve_pnl:.6f}")
        print(f"   tolerance        = {result.reconcile_tolerance}")
        print(f"   THIS IS A BUG. Do not trust any number above.")
        return 3

    print(f"  ✓ Reconciliation passed (diff < ${result.reconcile_tolerance})")

    # ---- Headline report ----
    print(f"\n{'─' * 70}")
    print(f"RESULTS")
    print(f"{'─' * 70}")
    print(f"  Trades:           {result.n_trades}")
    print(f"  Starting balance: ${result.starting_balance:,.2f}")
    print(f"  Ending balance:   ${result.ending_balance:,.2f}")
    print(f"  Sum realized PnL: ${result.sum_realized_pnl:+,.2f}")
    print(f"  Equity-curve PnL: ${result.equity_curve_pnl:+,.2f}")
    if result.starting_balance > 0:
        ret_pct = result.equity_curve_pnl / result.starting_balance * 100
        print(f"  Return:           {ret_pct:+.2f}%")

    if result.n_trades > 0:
        wins = [t for t in result.trades if t.realized_pnl > 0]
        losses = [t for t in result.trades if t.realized_pnl <= 0]
        gross_wins = sum(t.realized_pnl for t in wins)
        gross_losses = -sum(t.realized_pnl for t in losses)
        win_rate = len(wins) / result.n_trades * 100
        pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")
        avg_R = sum(t.r_multiple for t in result.trades) / result.n_trades
        print(f"  Wins / Losses:    {len(wins)} / {len(losses)}")
        print(f"  Win rate:         {win_rate:.1f}%")
        print(f"  Profit factor:    {pf:.2f}")
        print(f"  Avg R:            {avg_R:+.3f}")
        print(f"  Close reasons:")
        from collections import Counter
        c = Counter(t.close_reason for t in result.trades)
        for reason, n in c.most_common():
            print(f"    {reason:14s} {n:>4d}")

    # ---- Persist to SQLite ----
    storage.init_schema(args.db)
    run_id = "v2_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    storage.save_run(
        args.db, run_id,
        started_at_utc=datetime.now(timezone.utc).isoformat(),
        symbol=args.ticker, tf=args.tf, strategy_name=strategy.name,
        config_json=json.dumps(params.__dict__),
        starting_balance=args.balance,
    )
    trade_rows = [{
        "symbol": args.ticker, "direction": t.direction,
        "opened_at_utc": str(df["time"].iloc[t.entry_bar_idx]),
        "closed_at_utc": str(df["time"].iloc[t.exit_bar_idx]),
        "entry_price": t.entry_price, "stop_price": t.stop_price,
        "target_price": t.target_price, "exit_price": t.exit_price,
        "lots": t.lots, "realized_pnl": t.realized_pnl,
        "r_multiple": t.r_multiple, "close_reason": t.close_reason,
    } for t in result.trades]
    storage.save_trades(args.db, run_id, trade_rows)
    storage.finish_run(
        args.db, run_id,
        finished_at_utc=datetime.now(timezone.utc).isoformat(),
        ending_equity=result.ending_balance, n_trades=result.n_trades,
        sum_realized_pnl=result.sum_realized_pnl,
        equity_curve_pnl=result.equity_curve_pnl,
        reconciles=result.reconciles,
    )
    print(f"\n  Persisted to {args.db}  (run_id={run_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
