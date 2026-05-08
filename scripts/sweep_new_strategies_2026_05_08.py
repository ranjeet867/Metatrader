#!/usr/bin/env python3
"""sweep_new_strategies_2026_05_08.py — sweep the 3 new strategies
(double_bottom, engulfing_at_extreme, pivot_reaction_zone) on M15 + H1
across all tickers with M15/H1 parquets, OOS-validated against the same
hard gates the catalog uses.

Output: top deploy_safe survivors ranked by edge, ready to seed as paper.

Run:
    cd <repo> && .venv/bin/python scripts/sweep_new_strategies_2026_05_08.py
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

from strategies.double_bottom import DoubleBottom, DoubleBottomParams
from strategies.engulfing_at_extreme import (
    EngulfingAtExtreme, EngulfingAtExtremeParams,
)
from strategies.pivot_reaction_zone import (
    PivotReactionZone, PivotReactionParams,
)


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

# Min bars per TF — same thresholds as the existing sweep tool.
TF_MIN_BARS = {"M15": 7000, "H1": 4000}


def _build_variants():
    """All (label, factory) tuples to test. Each factory returns a fresh
    strategy instance so the sweep doesn't share state across tickers."""
    variants = []

    # double_bottom — long-only (W pattern is a bullish reversal),
    # tested with R:R 1:2 and 1:3, divergence on/off.
    for tgt in (2.0, 3.0):
        for div in (True, False):
            variants.append((
                f"double_bottom_RR_1:{int(tgt)}"
                + ("_div" if div else "_nodiv"),
                lambda t=tgt, d=div: DoubleBottom(DoubleBottomParams(
                    target_R_mult=t, require_divergence=d, long_only=True,
                )),
            ))

    # engulfing_at_extreme — bidir + long-only + with/without 200-EMA regime
    for tgt in (1.5, 2.0, 3.0):
        for lo in (False, True):
            label = (f"engulfing_at_extreme_RR_1:{int(tgt/1.5)}_"
                      f"{'long' if lo else 'bidir'}")
            variants.append((label,
                lambda t=tgt, lonly=lo: EngulfingAtExtreme(
                    EngulfingAtExtremeParams(
                        target_R_mult=t, long_only=lonly,
                    ))))
        # With 200-EMA regime (long-only by definition since regime
        # filter only allows trend-direction trades)
        variants.append((
            f"engulfing_at_extreme_RR_1:{int(tgt/1.5)}_long_+200ema",
            lambda t=tgt: EngulfingAtExtreme(EngulfingAtExtremeParams(
                target_R_mult=t, long_only=True, regime_ema_period=200,
            ))))

    # pivot_reaction_zone — bidir + long-only, test 2 zone widths
    for tgt in (1.5, 2.0):
        for zone_pct in (0.0025, 0.005):
            for lo in (False, True):
                label = (f"pivot_reaction_zone_RR_1:{int(tgt/1.5)}_"
                          f"zone{int(zone_pct*1000)}bp_"
                          f"{'long' if lo else 'bidir'}")
                variants.append((label,
                    lambda t=tgt, zp=zone_pct, lonly=lo: PivotReactionZone(
                        PivotReactionParams(
                            target_R_mult=t, zone_pct=zp, long_only=lonly,
                        ))))
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
    return dict(
        n_train=train.n_trades, n_test=test.n_trades,
        pf_train=round(pf_tr, 2), pf_test=round(pf_te, 2),
        r_test=round(test.avg_R, 3),
        wr_test=round(test.win_rate, 1),
        oos_dd_pct=round(oos_dd, 2),
        recov_oos=(round(rec, 1) if rec is not None else None),
        net_pnl=round(res.sum_realized_pnl, 0),
        deploy_safe=(len(gates) == 0),
        gates_failed=gates,
    )


def main():
    variants = _build_variants()
    print(f"Strategy variants: {len(variants)}")
    print(f"Tickers: {len(TICKERS)}")
    print("Timeframes: M15, H1")
    print("Cell budget: ~%d" % (len(variants) * len(TICKERS) * 2))
    print()

    survivors: list[dict] = []
    n_total = 0
    n_pass = 0

    for ticker, mpu in TICKERS:
        for tf in ("M15", "H1"):
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
                except Exception as e:
                    print(f"  ❌ {label:50}  {type(e).__name__}: {e}")
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
                    print(f"  ✅ {label:50} n={m['n_test']:>4} "
                          f"PF={m['pf_test']:.2f} R={m['r_test']:+.2f} "
                          f"WR={m['wr_test']:.0f}% DD={m['oos_dd_pct']:.1f}% "
                          f"recov={rec_s}")

    survivors.sort(key=lambda r: -(r["pf_test"]
                                       * max(0.001, r["r_test"])))

    print()
    print("=" * 110)
    print(f"NEW-STRATEGY SWEEP DONE — {n_pass} of {n_total} cells "
          f"passed OOS hard gate")
    print("=" * 110)
    print(f"{'Ticker':12} {'TF':4} {'Variant':55} "
          f"{'n':>4} {'PF':>5} {'R':>6} {'WR%':>5} {'DD%':>5} {'recov':>7}")
    for r in survivors[:25]:
        rec_s = (f"{r['recov_oos']:.0f}d"
                  if r['recov_oos'] is not None else "?")
        print(f"  {r['ticker']:10} {r['tf']:4} {r['label'][:55]:55} "
              f"{r['n_test']:>4} {r['pf_test']:>5.2f} "
              f"{r['r_test']:>+6.2f} {r['wr_test']:>4.0f}% "
              f"{r['oos_dd_pct']:>4.1f}% {rec_s:>7}")

    if not survivors:
        print("(no deploy-safe cells found)")


if __name__ == "__main__":
    main()
