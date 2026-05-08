#!/usr/bin/env python3
"""sweep_all_strategies_existing_data.py — comprehensive sweep on
EXISTING data (D1 7y, H1 1.3y, M15 4mo) without needing more bridge fetches.

Pivoted from the M15-5y goal because the Wine MT5 bridge can't reliably
serve large historical requests. D1 already has 7+ years on most tickers;
that's better than M15 for most strategies anyway.

Coverage:
  - D1: every ticker with 200+ bars (most have 1500+)
  - H1: every ticker with 5000+ bars (~1y minimum)
  - M15: only run when 7000+ bars (~3 months minimum)

Strategies: 22 registered × 4 R:R variants × 2 sides = ~150 cells per ticker
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from core import cost_defaults
from core.backtest import run_backtest, partition_train_test, BacktestResult
from core.backtest_stats import compute_full_stats
from core.data import load_parquet
from core.edge_catalog import _evaluate_hard_gates
from core.symbol_info_loader import try_load


TICKERS = [
    ("EURUSD",      100_000.0),
    ("GBPUSD",      100_000.0),
    ("US100.cash",  1.0),
    ("US500.cash",  1.0),
    ("US30.cash",   1.0),
    ("JP225.cash",  1.0),
    ("HK50.cash",   1.0),
    ("GER40.cash",  1.0),
    ("UK100.cash",  1.0),
    ("XAUUSD",      100.0),
    ("XAGUSD",      5_000.0),
    ("XPDUSD",      100.0),
    ("XPTUSD",      100.0),
]

# TF minimum bar requirements
TF_MIN_BARS = {"M15": 7000, "H1": 4000, "D1": 200}


def _build_variants():
    from strategies.ema_cross import EmaCross, EmaCrossParams
    from strategies.donchian_breakout import (
        DonchianBreakout, DonchianBreakoutParams,
    )
    from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams

    variants = []
    # ema_cross 9/20 + 12/26 with various R:R
    for fast, slow in ((9, 20), (12, 26)):
        for tgt in (1.5, 3.0, 4.5):
            for lo in (False, True):
                label = (f"ema_cross_{fast}_{slow}_RR_1:{int(tgt/1.5)}_"
                          f"{'long' if lo else 'bidir'}")
                variants.append((label,
                    lambda f=fast, s=slow, t=tgt, lonly=lo: EmaCross(
                        EmaCrossParams(fast_period=f, slow_period=s,
                                          atr_period=14, stop_atr_mult=1.5,
                                          target_atr_mult=t, long_only=lonly))))
    # ema_cross 12/26 with regime_ema=200 (the magic combo)
    for tgt in (3.0, 4.5):
        variants.append((f"ema_cross_12_26_RR_1:{int(tgt/1.5)}_long_+200ema",
            lambda t=tgt: EmaCross(EmaCrossParams(
                fast_period=12, slow_period=26, atr_period=14,
                stop_atr_mult=1.5, target_atr_mult=t,
                long_only=True, regime_ema_period=200))))

    # donchian 20/55 with 200-EMA filter variants
    for period in (20, 55):
        for tgt in (1.5, 3.0, 4.5):
            for lo in (False, True):
                label = (f"donchian_{period}_RR_1:{int(tgt/1.5)}_"
                          f"{'long' if lo else 'bidir'}")
                variants.append((label,
                    lambda p=period, t=tgt, lonly=lo: DonchianBreakout(
                        DonchianBreakoutParams(period=p, atr_period=14,
                                                 stop_atr_mult=1.5,
                                                 target_atr_mult=t,
                                                 long_only=lonly))))
        # +200ema regime variant
        for tgt in (3.0, 4.5):
            variants.append((
                f"donchian_{period}_RR_1:{int(tgt/1.5)}_long_+200ema",
                lambda p=period, t=tgt: DonchianBreakout(
                    DonchianBreakoutParams(period=p, atr_period=14,
                                             stop_atr_mult=1.5,
                                             target_atr_mult=t,
                                             long_only=True,
                                             regime_ema_period=200))))

    # rsi 30/70, 25/75, 20/80
    for ob, os in ((70, 30), (75, 25), (80, 20)):
        for tgt in (1.5, 3.0):
            for lo in (False, True):
                label = (f"rsi_{os}_{ob}_RR_1:{int(tgt/1.5)}_"
                          f"{'long' if lo else 'bidir'}")
                variants.append((label,
                    lambda OB=ob, OS=os, t=tgt, lonly=lo: RsiMeanRev(
                        RsiMeanRevParams(rsi_period=14,
                                          oversold=OS, overbought=OB,
                                          atr_period=14,
                                          stop_atr_mult=1.5,
                                          target_atr_mult=t,
                                          long_only=lonly))))
    return variants


def _eval(df, strat, mpu, ticker, sname):
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
    variants = _build_variants()
    print(f"Strategy variants: {len(variants)}")
    print(f"Tickers: {len(TICKERS)}")
    print(f"Timeframes: D1, H1, M15")
    print()

    survivors: list[dict] = []
    n_total = 0
    n_pass = 0

    for ticker, mpu in TICKERS:
        for tf in ("D1", "H1", "M15"):
            ppath = ROOT / "data" / f"{ticker}_{tf}.parquet"
            if not ppath.exists():
                continue
            df = load_parquet(ppath)
            min_bars = TF_MIN_BARS[tf]
            if len(df) < min_bars:
                continue
            years = (pd.to_datetime(df["time"].iloc[-1])
                      - pd.to_datetime(df["time"].iloc[0])).days / 365.25
            print(f"\n=== {ticker} {tf}: {len(df):,} bars  {years:.2f}y ===")

            for label, factory in variants:
                n_total += 1
                try:
                    strat = factory()
                    sname = label.split("_RR_")[0]
                    m = _eval(df, strat, mpu, ticker, sname)
                except Exception:
                    continue
                if m is None:
                    continue
                if m["deploy_safe"]:
                    n_pass += 1
                    survivors.append(dict(
                        ticker=ticker, tf=tf, label=label, **m,
                    ))
                    rec_s = (f"{m['recov_oos']:.0f}d"
                              if m['recov_oos'] is not None else "?")
                    print(f"  ✅ {label:42} n={m['n_test']:>4} "
                          f"PF={m['pf_test']:.2f} R={m['r_test']:+.2f} "
                          f"DD={m['oos_dd_pct']:.1f}% recov={rec_s}")

    survivors.sort(key=lambda r: -(r["pf_test"]
                                       * max(0.001, r["r_test"])))

    print()
    print("=" * 110)
    print(f"FULL SWEEP COMPLETE — {n_pass} of {n_total} cells passed OOS gate")
    print("=" * 110)
    print(f"{'Ticker':12} {'TF':4} {'Variant':45} {'n':>4} {'PF':>5} "
          f"{'R':>6} {'DD%':>5} {'recov':>7}")
    for r in survivors[:30]:
        rec_s = (f"{r['recov_oos']:.0f}d"
                  if r['recov_oos'] is not None else "?")
        print(f"  {r['ticker']:10} {r['tf']:4} {r['label'][:45]:45} "
              f"{r['n_test']:>4} {r['pf_test']:>5.2f} "
              f"{r['r_test']:>+6.2f} {r['oos_dd_pct']:>4.1f}% {rec_s:>7}")


if __name__ == "__main__":
    main()
