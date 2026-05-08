#!/usr/bin/env python3
"""sweep_new_strategies_h1_d1.py — run the same 3 new strategies
(double_bottom, engulfing_at_extreme, pivot_reaction_zone) on H1 + D1
across all tickers. Same OOS hard gates as the catalog.

The earlier sweep (2026-05-08) only covered M15 + H1; the H1 cell-pool
saw fewer survivors than M15. D1 was excluded because the M15-tuned
parameters (pivot_window=5, min_separation=8, max_separation=60) translate
to ~60 trading days of context — which is fine on D1 too. So we re-run
H1 with the same params + add D1.

Output: top deploy-safe survivors ranked by edge_score proxy
(PF × max(0.001, R)).

Run:
    cd <repo> && .venv/bin/python scripts/sweep_new_strategies_h1_d1.py
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

# H1 needs ~4000 bars (~6mo); D1 needs ~200 bars (~9mo) but we want at
# least 500 (~2y) for any strategy with min_separation=8 + max_separation=60
# to have a meaningful test slice.
TF_MIN_BARS = {"H1": 4000, "D1": 500}


def _build_variants():
    variants = []
    # double_bottom — long-only, NODIV (winner pattern from M15 sweep)
    # plus one DIV variant for completeness.
    for tgt in (1.5, 2.0, 3.0):
        for div in (False, True):
            variants.append((
                f"double_bottom_RR_1:{int(tgt)}"
                + ("_div" if div else "_nodiv"),
                lambda t=tgt, d=div: DoubleBottom(DoubleBottomParams(
                    target_R_mult=t, require_divergence=d, long_only=True,
                )),
            ))
    # engulfing_at_extreme — bidir + long + 200-EMA regime
    for tgt in (1.5, 2.0, 3.0):
        for lo in (False, True):
            label = (f"engulfing_at_extreme_RR_1:{int(tgt/1.5)}_"
                      f"{'long' if lo else 'bidir'}")
            variants.append((label,
                lambda t=tgt, lonly=lo: EngulfingAtExtreme(
                    EngulfingAtExtremeParams(
                        target_R_mult=t, long_only=lonly,
                    ))))
        variants.append((
            f"engulfing_at_extreme_RR_1:{int(tgt/1.5)}_long_+200ema",
            lambda t=tgt: EngulfingAtExtreme(EngulfingAtExtremeParams(
                target_R_mult=t, long_only=True, regime_ema_period=200,
            ))))
    # pivot_reaction_zone — long-only + bidir, 2 zone widths
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
        deploy_safe=(len(gates) == 0),
    )


def main():
    variants = _build_variants()
    print(f"Strategy variants: {len(variants)}")
    print(f"Tickers: {len(TICKERS)}")
    print("Timeframes: H1, D1")
    print("Cell budget: ~%d" % (len(variants) * len(TICKERS) * 2))
    print()

    survivors: list[dict] = []
    n_total = 0
    n_pass = 0

    for ticker, mpu in TICKERS:
        for tf in ("H1", "D1"):
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
                    print(f"  ✅ {label:50} n={m['n_test']:>4} "
                          f"PF={m['pf_test']:.2f} R={m['r_test']:+.2f} "
                          f"WR={m['wr_test']:.0f}% DD={m['oos_dd_pct']:.1f}% "
                          f"recov={rec_s}")

    survivors.sort(key=lambda r: -(r["pf_test"]
                                       * max(0.001, r["r_test"])))

    print()
    print("=" * 110)
    print(f"H1+D1 SWEEP — {n_pass} of {n_total} cells passed OOS hard gate")
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
        print("(no deploy-safe cells found on H1 or D1)")


if __name__ == "__main__":
    main()
