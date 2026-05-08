#!/usr/bin/env python3
"""validate_promote_candidates_oos.py — verify the top promotion
candidates pass the OOS hard-gates through the production backtester.

Candidates:
  1. plain S/R XAUUSD D1 R:R 1:2
  2. plain S/R XPTUSD D1 R:R 1:2
  3. ema_filter S/R XPTUSD H1 R:R 1:2
  4. ema_filter S/R XPDUSD H1 R:R 1:2
  5. donchian_55 XAUUSD H1 RR_1:3 long  (variant of existing cell)

Run each through core.backtest.run_backtest with cost_defaults; split
60/40 by entry-bar; evaluate hard gates against OOS slice. Print a
clear pass/fail per cell and which ones to promote.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import cost_defaults
from core.backtest import run_backtest, partition_train_test
from core.backtest_stats import compute_full_stats
from core.data import load_parquet
from core.edge_catalog import (
    _evaluate_hard_gates as evaluate_hard_gates,
)
from core.symbol_info_loader import try_load

from strategies.support_resistance import (
    SupportResistance, SupportResistanceParams,
)
from strategies.donchian_breakout import (
    DonchianBreakout, DonchianBreakoutParams,
)


def _build(name, ticker, tf, factory, params, mpu, lots_fallback):
    df = load_parquet(ROOT / "data" / f"{ticker}_{tf}.parquet")
    sym = try_load(ticker)
    sigs = factory(params).signals(df)
    res = run_backtest(
        df, sigs, starting_balance=100_000.0, lots=0.0,
        money_per_unit_price=mpu,
        commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        risk_pct=0.30, symbol_info=sym, symbol=ticker,
    )
    train, test = partition_train_test(res, train_pct=0.6,
                                          n_bars=len(df))
    # OOS-only stats for recovery
    from core.backtest import BacktestResult
    test_trades = [t for t in res.trades
                   if t.entry_bar_idx >= res.equity_curve.index[0]]
    # Use the simple split we have already
    split_idx = int(len(df) * 0.6)
    test_trades = [t for t in res.trades if t.entry_bar_idx >= split_idx]
    if test_trades:
        synth = BacktestResult(
            trades=test_trades,
            equity_curve=res.equity_curve,
            starting_balance=100_000.0,
            ending_balance=100_000.0
                            + sum(t.realized_pnl for t in test_trades),
            sum_realized_pnl=sum(t.realized_pnl for t in test_trades),
            equity_curve_pnl=sum(t.realized_pnl for t in test_trades),
            reconciles=True,
        )
        oos_stats = compute_full_stats(synth, starting_balance=100_000.0)
        recov_oos = oos_stats.recovery_duration_days
        oos_dd_pct = oos_stats.max_dd_pct
    else:
        recov_oos = None
        oos_dd_pct = 0.0
    pf_te = (99.99 if test.profit_factor == float("inf")
              else test.profit_factor)
    pf_tr = (99.99 if train.profit_factor == float("inf")
              else train.profit_factor)
    gates = evaluate_hard_gates(
        overall_pf=pf_te,
        test_pf=pf_te,
        test_r=test.avg_R,
        recovery_days=recov_oos,
        n_test=test.n_trades,
        strategy_name=name,
    )
    return dict(
        name=name, ticker=ticker, tf=tf,
        n_train=train.n_trades, n_test=test.n_trades,
        pf_train=round(pf_tr, 2), pf_test=round(pf_te, 2),
        r_train=round(train.avg_R, 3), r_test=round(test.avg_R, 3),
        wr_test=round(test.win_rate, 1),
        oos_dd_pct=round(oos_dd_pct, 2),
        recov_oos=(round(recov_oos, 1) if recov_oos is not None else None),
        net_pnl=round(res.sum_realized_pnl, 0),
        gates_failed=gates,
        deploy_safe=(len(gates) == 0),
        params=params,
        mpu=mpu,
    )


def main():
    cands = [
        ("support_resistance", "XAUUSD", "D1", 100.0, 0.30,
         SupportResistance,
         SupportResistanceParams(variant="plain", rr_target=2.0)),
        ("support_resistance", "XPTUSD", "D1", 100.0, 0.25,
         SupportResistance,
         SupportResistanceParams(variant="plain", rr_target=2.0)),
        ("support_resistance", "XPTUSD", "H1", 100.0, 0.25,
         SupportResistance,
         SupportResistanceParams(variant="ema_filter", rr_target=2.0)),
        ("support_resistance", "XPDUSD", "H1", 100.0, 0.20,
         SupportResistance,
         SupportResistanceParams(variant="ema_filter", rr_target=2.0)),
        ("donchian_55", "XAUUSD", "H1", 100.0, 0.30,
         DonchianBreakout,
         DonchianBreakoutParams(period=55, atr_period=14,
                                  stop_atr_mult=1.5, target_atr_mult=4.5,
                                  long_only=True)),
    ]

    print("=" * 110)
    print("OOS validation via production backtester")
    print("=" * 110)
    print(f"{'Strategy':22} {'Ticker':10} {'TF':4} "
          f"{'n_tr':>5} {'n_te':>5} {'PF_tr':>6} {'PF_te':>6} "
          f"{'R_te':>6} {'OOS_DD':>7} {'recov':>7} {'verdict':>10}")
    print("-" * 110)
    survivors = []
    for name, ticker, tf, mpu, _lots, factory, params in cands:
        try:
            r = _build(name, ticker, tf, factory, params, mpu, 0.10)
        except Exception as e:
            print(f"{name:22} {ticker:10} {tf:4}  ERROR: {e!r}")
            continue
        verdict = "✅ PASS" if r["deploy_safe"] else "🚫 FAIL"
        rec_s = (f"{r['recov_oos']:.0f}d"
                  if r['recov_oos'] is not None else "?")
        print(f"{name[:22]:22} {ticker[:10]:10} {tf:4} "
              f"{r['n_train']:>5} {r['n_test']:>5} "
              f"{r['pf_train']:>6.2f} {r['pf_test']:>6.2f} "
              f"{r['r_test']:>+6.2f} "
              f"{r['oos_dd_pct']:>5.1f}% "
              f"{rec_s:>7} {verdict:>10}")
        if r["gates_failed"]:
            for g in r["gates_failed"]:
                print(f"  └── {g[:90]}")
        if r["deploy_safe"]:
            survivors.append((name, ticker, tf, mpu, factory, params, r))

    print()
    print("=" * 110)
    print(f"OOS-VERIFIED PROMOTION LIST ({len(survivors)} cells)")
    print("=" * 110)
    for name, ticker, tf, mpu, _f, params, r in survivors:
        print(f"  ✅ {name} {ticker} {tf}  "
              f"PF_oos={r['pf_test']:.2f}  R_oos={r['r_test']:+.2f}  "
              f"recov_oos={r['recov_oos']:.0f}d  net=${r['net_pnl']:+,.0f}")
    return 0 if survivors else 1


if __name__ == "__main__":
    sys.exit(main())
