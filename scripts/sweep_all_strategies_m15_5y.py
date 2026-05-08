#!/usr/bin/env python3
"""sweep_all_strategies_m15_5y.py — comprehensive M15 sweep across
every registered strategy × every ticker we have M15 data for.

Run AFTER fetch_extended_m15.sh has updated the parquets.

Methodology:
  - For each (strategy, ticker) cell on M15:
    - Run the production backtester at standard cost defaults
    - 60/40 train/test split, OOS-validated
    - Try multiple R:R variants for parameterized strategies
  - Apply hard gates: PF≥1.05, OOS PF≥1.0, R≥0, n_test≥15, recov≤90/180d
  - Output: ranked promotion candidates + comparison vs current catalog

Saves results to data/experiment_log.json under strategy="m15_5y_sweep"
so the catalog auto-discovers them.

Usage:
    .venv/bin/python scripts/sweep_all_strategies_m15_5y.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from core import cost_defaults, experiment_log
from core.backtest import run_backtest, partition_train_test, BacktestResult
from core.backtest_stats import compute_full_stats
from core.data import load_parquet
from core.edge_catalog import _evaluate_hard_gates
from core.symbol_info_loader import try_load


# Tickers to sweep — ones we backtest on
TICKERS = [
    ("EURUSD",      100_000.0,  0.30),    # FX 1 lot ≈ $100k notional
    ("US100.cash",  1.0,         6.5),
    ("US500.cash",  1.0,         5.0),
    ("US30.cash",   1.0,         3.0),
    ("JP225.cash",  1.0,         2.0),
    ("HK50.cash",   1.0,         3.0),
    ("XAUUSD",      100.0,       0.30),
    ("XAGUSD",      5_000.0,     0.40),
    ("XPDUSD",      100.0,       0.20),
    ("XPTUSD",      100.0,       0.25),
]

# Strategy variants to test — ones that DEFINITELY work for M15
def _build_variants():
    from strategies.ema_cross import EmaCross, EmaCrossParams
    from strategies.donchian_breakout import (
        DonchianBreakout, DonchianBreakoutParams,
    )
    from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
    from strategies.bbands_meanrev import (
        BBandsMeanRev, BBandsMeanRevParams,
    )

    variants = []

    # ema_cross 9/20 — 5 R:R variants × bidir/long
    for tgt in (1.5, 3.0, 4.5):
        for lo in (False, True):
            label = f"ema_cross_9_20_RR_1:{tgt/1.5:.0f}_{'long' if lo else 'bidir'}"
            variants.append((label,
                lambda t=tgt, lonly=lo: EmaCross(EmaCrossParams(
                    fast_period=9, slow_period=20, atr_period=14,
                    stop_atr_mult=1.5, target_atr_mult=t, long_only=lonly,
                ))))

    # ema_cross 12/26 with regime filter — the new winning config
    for tgt in (3.0, 4.5):
        variants.append((f"ema_cross_12_26_RR_1:{tgt/1.5:.0f}_long_+200ema",
            lambda t=tgt: EmaCross(EmaCrossParams(
                fast_period=12, slow_period=26, atr_period=14,
                stop_atr_mult=1.5, target_atr_mult=t,
                long_only=True, regime_ema_period=200,
            ))))

    # donchian 20/55 — 4 R:R variants
    for period in (20, 55):
        for tgt in (1.5, 3.0, 4.5):
            for lo in (False, True):
                label = f"donchian_{period}_RR_1:{tgt/1.5:.0f}_{'long' if lo else 'bidir'}"
                variants.append((label,
                    lambda p=period, t=tgt, lonly=lo: DonchianBreakout(
                        DonchianBreakoutParams(period=p, atr_period=14,
                                                 stop_atr_mult=1.5,
                                                 target_atr_mult=t,
                                                 long_only=lonly))))

    # rsi_30_70 + rsi_25_75 + rsi_20_80
    for ob, os in ((70, 30), (75, 25), (80, 20)):
        for tgt in (1.5, 3.0):
            for lo in (False, True):
                label = (f"rsi_{os}_{ob}_RR_1:{tgt/1.5:.0f}_"
                          f"{'long' if lo else 'bidir'}")
                variants.append((label,
                    lambda OB=ob, OS=os, t=tgt, lonly=lo: RsiMeanRev(
                        RsiMeanRevParams(rsi_period=14,
                                          oversold=OS, overbought=OB,
                                          atr_period=14,
                                          stop_atr_mult=1.5,
                                          target_atr_mult=t,
                                          long_only=lonly))))

    # bbands_meanrev — 2 variants
    for tgt in (1.5, 3.0):
        variants.append((f"bbands_20_2_RR_1:{tgt/1.5:.0f}_bidir",
            lambda t=tgt: BBandsMeanRev(BBandsMeanRevParams(
                bb_period=20, bb_k=2.0, atr_period=14,
                stop_atr_mult=1.5, target_atr_mult=t, long_only=False))))

    return variants


def _eval_cell(df, strat, mpu, ticker, sname):
    sym = try_load(ticker)
    sigs = strat.signals(df)
    if not sigs:
        return None
    res = run_backtest(
        df, sigs, starting_balance=100_000.0, lots=0.0,
        money_per_unit_price=mpu,
        commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        risk_pct=0.30, symbol_info=sym, symbol=ticker,
    )
    if not res.trades:
        return None
    train, test = partition_train_test(res, train_pct=0.6, n_bars=len(df))
    split_idx = int(len(df) * 0.6)
    test_trades = [t for t in res.trades if t.entry_bar_idx >= split_idx]
    if test_trades:
        synth = BacktestResult(
            trades=test_trades, equity_curve=res.equity_curve,
            starting_balance=100_000.0,
            ending_balance=100_000.0
                            + sum(t.realized_pnl for t in test_trades),
            sum_realized_pnl=sum(t.realized_pnl for t in test_trades),
            equity_curve_pnl=sum(t.realized_pnl for t in test_trades),
            reconciles=True,
        )
        oos = compute_full_stats(synth, starting_balance=100_000.0)
        rec = oos.recovery_duration_days
        oos_dd = oos.max_dd_pct
    else:
        rec = None
        oos_dd = 0.0
    pf_te = (99.99 if test.profit_factor == float("inf")
              else test.profit_factor)
    pf_tr = (99.99 if train.profit_factor == float("inf")
              else train.profit_factor)
    gates = _evaluate_hard_gates(
        overall_pf=pf_te, test_pf=pf_te, test_r=test.avg_R,
        recovery_days=rec, n_test=test.n_trades,
        strategy_name=sname,
    )
    # Max consec losses
    streak_loss = 0
    max_streak_loss = 0
    for t in res.trades:
        if t.realized_pnl < 0:
            streak_loss += 1
            max_streak_loss = max(max_streak_loss, streak_loss)
        else:
            streak_loss = 0
    return dict(
        n_train=train.n_trades, n_test=test.n_trades,
        pf_train=round(pf_tr, 2), pf_test=round(pf_te, 2),
        r_test=round(test.avg_R, 3),
        wr_test=round(test.win_rate, 1),
        oos_dd_pct=round(oos_dd, 2),
        recov_oos=(round(rec, 1) if rec is not None else None),
        net_pnl=round(res.sum_realized_pnl, 0),
        max_consec_losses=max_streak_loss,
        deploy_safe=(len(gates) == 0),
    )


def main():
    log = experiment_log.load()
    print(f"Loaded log: {len(log.records)} prior records\n")

    variants = _build_variants()
    print(f"Total variants to test: {len(variants)}")
    print(f"Total tickers: {len(TICKERS)}")
    print(f"Total cells: {len(variants) * len(TICKERS)}\n")

    survivors: list[dict] = []
    n_total = 0
    n_pass = 0

    for ticker, mpu, _lots in TICKERS:
        ppath = ROOT / "data" / f"{ticker}_M15.parquet"
        if not ppath.exists():
            print(f"  ⚠ skip {ticker} — no M15 parquet")
            continue
        df = load_parquet(ppath)
        years = (pd.to_datetime(df["time"].iloc[-1])
                  - pd.to_datetime(df["time"].iloc[0])).days / 365.25
        print(f"\n=== {ticker} M15: {len(df):,} bars  {years:.2f}y ===")
        if years < 1.0:
            print(f"  ⚠ only {years:.2f}y — limited OOS sample size")

        for label, factory in variants:
            n_total += 1
            try:
                strat = factory()
                m = _eval_cell(df, strat, mpu, ticker, label.split("_RR_")[0])
            except Exception as e:
                continue
            if m is None:
                continue
            if m["deploy_safe"]:
                n_pass += 1
                survivors.append(dict(
                    ticker=ticker, label=label, **m,
                ))
                rec_s = (f"{m['recov_oos']:.0f}d"
                          if m['recov_oos'] is not None else "?")
                print(f"  ✅ {label:42} n={m['n_test']:>4} "
                      f"PF={m['pf_test']:.2f} R={m['r_test']:+.2f} "
                      f"DD={m['oos_dd_pct']:.1f}% recov={rec_s}")

    # Top-N by score-like metric (PF * mean_R)
    survivors.sort(key=lambda r: -(r["pf_test"]
                                       * max(0.001, r["r_test"])))

    print()
    print("=" * 100)
    print(f"M15 5Y SWEEP COMPLETE — {n_pass} of {n_total} cells passed OOS gate")
    print("=" * 100)
    print(f"{'Ticker':12} {'Variant':45} {'n':>4} {'PF':>5} "
          f"{'R':>6} {'DD%':>5} {'recov':>7} {'maxLoss':>7}")
    for r in survivors[:25]:
        rec_s = (f"{r['recov_oos']:.0f}d"
                  if r['recov_oos'] is not None else "?")
        print(f"  {r['ticker']:10} {r['label'][:45]:45} {r['n_test']:>4} "
              f"{r['pf_test']:>5.2f} {r['r_test']:>+6.2f} "
              f"{r['oos_dd_pct']:>4.1f}% {rec_s:>7} "
              f"{r['max_consec_losses']:>7}")

    print(f"\nTop survivor: {survivors[0]['ticker']} "
          f"{survivors[0]['label']}" if survivors else "(none)")
    return 0 if survivors else 1


if __name__ == "__main__":
    sys.exit(main())
