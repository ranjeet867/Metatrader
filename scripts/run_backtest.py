#!/usr/bin/env python3
"""
run_backtest.py — end-to-end backtest on a locked parquet, ANY strategy.

Reads data/<ticker>_<tf>.parquet, builds the requested strategy, runs the
reconciliation-enforced backtester, prints a clean report, asserts
reconciliation passes.

Usage:
  python scripts/run_backtest.py
  python scripts/run_backtest.py --ticker US100.cash --tf D1 --strategy vol_breakout --long-only
  python scripts/run_backtest.py --strategy ema_cross --fast 12 --slow 26
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import partition_train_test, run_backtest
from core.data import load_parquet
from core import storage
from dashboards.control import discover_strategies
from strategies.ema_cross import EmaCross, EmaCrossParams


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="US100.cash")
    ap.add_argument("--tf", default="H1")
    ap.add_argument("--strategy", default="ema_cross",
                    help="strategy name (auto-discovered from strategies/)")
    ap.add_argument("--params-json", default=None,
                    help="JSON dict of param overrides for the strategy (optional)")
    # All cost defaults come from core.cost_defaults — same constants
    # the Backtest page + sweep + rebaseline use. Override here only
    # for ad-hoc what-if runs; leave alone for catalog-consistent runs.
    from core import cost_defaults as _cd
    ap.add_argument("--balance", type=float,
                    default=_cd.DEFAULT_STARTING_BALANCE_USD)
    ap.add_argument("--lots", type=float, default=0.1)
    ap.add_argument("--money-per-unit", type=float, default=1.0,
                    help="$ per 1.0 price unit per 1 lot (US100.cash default = $1)")
    ap.add_argument("--commission-per-trade", type=float,
                    default=_cd.DEFAULT_COMMISSION_USD,
                    help="$ deducted from realized_pnl per closed trade (round-trip)")
    ap.add_argument("--slippage-atr-frac", type=float,
                    default=_cd.DEFAULT_SLIPPAGE_ATR_FRAC,
                    help="per-fill slippage as fraction of ATR(14) at the fill bar")
    ap.add_argument("--train-pct", type=float,
                    default=_cd.DEFAULT_TRAIN_PCT,
                    help="fraction of bars considered IN-SAMPLE for the train/test split")
    # ema_cross-specific shortcut flags (only used if --strategy ema_cross)
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

    # ---- Build the requested strategy ----
    if args.strategy == "ema_cross":
        # Start from CLI args / defaults, then let --params-json override.
        # Earlier this branch silently IGNORED --params-json — so any
        # custom target_atr_mult passed via JSON would be replaced by
        # the default 3.0, producing R:R 2.0 results when the user
        # asked for R:R 2.7. Now JSON wins, matching the else branch.
        kwargs = dict(
            fast_period=args.fast, slow_period=args.slow,
            atr_period=14,
            stop_atr_mult=args.stop_atr_mult,
            target_atr_mult=args.target_atr_mult,
            long_only=args.long_only,
        )
        if args.params_json:
            try:
                overrides = json.loads(args.params_json)
            except json.JSONDecodeError as e:
                print(f"ERROR: --params-json is not valid JSON: {e}")
                return 5
            field_names = {f.name for f in dataclasses.fields(EmaCrossParams)}
            bad = set(overrides) - field_names
            if bad:
                print(f"ERROR: unknown params for ema_cross: {bad}")
                return 5
            kwargs.update(overrides)
        params = EmaCrossParams(**kwargs)
        strategy = EmaCross(params)
    else:
        strats = discover_strategies()
        if args.strategy not in strats:
            print(f"ERROR: unknown strategy '{args.strategy}'. "
                  f"Available: {sorted(strats.keys())}")
            return 5
        StratCls, ParamsCls = strats[args.strategy]
        kwargs: dict = {}
        if ParamsCls is not None:
            field_names = {f.name for f in dataclasses.fields(ParamsCls)}
            if "long_only" in field_names and args.long_only:
                kwargs["long_only"] = True
            if args.params_json:
                try:
                    overrides = json.loads(args.params_json)
                except json.JSONDecodeError as e:
                    print(f"ERROR: --params-json is not valid JSON: {e}")
                    return 5
                bad = set(overrides) - field_names
                if bad:
                    print(f"ERROR: unknown params for {args.strategy}: {bad}")
                    return 5
                kwargs.update(overrides)
            params = ParamsCls(**kwargs)
            strategy = StratCls(params)
        else:
            params = None
            strategy = StratCls()
    signals = strategy.signals(df)
    print(f"\nStrategy: {strategy.name}  params={params}")
    print(f"  Signals generated: {len(signals)}")

    print(f"\nRunning backtest...")
    result = run_backtest(
        df, signals,
        starting_balance=args.balance,
        lots=args.lots,
        money_per_unit_price=args.money_per_unit,
        commission_per_trade=args.commission_per_trade,
        slippage_per_fill_atr_frac=args.slippage_atr_frac,
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
        # Canonical convention — match summarize_trades + edge_catalog
        # so CLI output, dashboard catalog, and Backtest page all
        # agree on the same definition of win/loss/scratch.
        wins = [t for t in result.trades if t.realized_pnl > 0]
        losses = [t for t in result.trades if t.realized_pnl < 0]
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

        # ---- OOS partition report ----
        train, test = partition_train_test(result, args.train_pct, n_bars=len(df))
        split_idx = int(len(df) * args.train_pct)
        split_time = df["time"].iloc[split_idx] if split_idx < len(df) else df["time"].iloc[-1]
        print(f"\n  ── OOS partition (train_pct={args.train_pct}, split @ bar {split_idx}, "
              f"{split_time}) ──")
        for m in (train, test):
            tr_pf = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
            print(f"    {m.label:<5s} n={m.n_trades:>4d}  PF={tr_pf:>5s}  "
                  f"avg_R={m.avg_R:>+6.3f}  win%={m.win_rate:>5.1f}  "
                  f"$={m.sum_pnl:>+10,.2f}")
        # Sum invariant — partition splits MUST conserve total PnL.
        # Tolerance scales with the magnitude of the sum so cumulative
        # FP error on $100k+ runs doesn't false-alarm. 1e-9 × |total|
        # is conservative; pure-FP error rarely exceeds 1e-12 × |total|.
        diff = train.sum_pnl + test.sum_pnl - result.sum_realized_pnl
        tol = max(1e-3, 1e-9 * abs(result.sum_realized_pnl))
        if abs(diff) > tol:
            print(f"  ⛔ partition sum invariant broken: "
                  f"train+test - total = {diff} (tol ${tol:.6f})")
            return 4

    # ---- Persist to SQLite ----
    storage.init_schema(args.db)
    # 12 hex chars = 48 bits of entropy. Pre-fix used [:6] (24 bits) which
    # could realistically collide under concurrent rebaseline runs (~16M
    # variants per second). 48 bits gives ~2.8e14 — astronomical at our
    # tx rate.
    run_id = "v2_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:12]
    storage.save_run(
        args.db, run_id,
        started_at_utc=datetime.now(timezone.utc).isoformat(),
        symbol=args.ticker, tf=args.tf, strategy_name=strategy.name,
        config_json=json.dumps(
            dataclasses.asdict(params) if params is not None else {}
        ),
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
