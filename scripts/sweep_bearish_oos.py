#!/usr/bin/env python3
"""sweep_bearish_oos.py — OOS-validate the 3 bearish-mirror strategies
across metals + indices D1 + H1, with bull-vs-bear comparison.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import cost_defaults
from core.backtest import run_backtest, partition_train_test, BacktestResult
from core.backtest_stats import compute_full_stats
from core.data import load_parquet
from core.edge_catalog import _evaluate_hard_gates
from core.symbol_info_loader import try_load

from strategies.last_week_low import LastWeekLow, LastWeekLowParams
from strategies.last_week_high import LastWeekHigh, LastWeekHighParams
from strategies.support_resistance import (
    SupportResistance, SupportResistanceParams,
)
from strategies.support_resistance_short import (
    SupportResistanceShort, SupportResistanceShortParams,
)
from strategies.ema_spread_pullback import (
    EmaSpreadPullback, EmaSpreadPullbackParams,
)
from strategies.ema_spread_pullback_short import (
    EmaSpreadPullbackShort, EmaSpreadPullbackShortParams,
)


TICKERS = [
    ("XAUUSD",     100.0,    0.30, "D1"),
    ("XAGUSD",     5_000.0,  0.40, "D1"),
    ("XPDUSD",     100.0,    0.20, "D1"),
    ("XPTUSD",     100.0,    0.25, "D1"),
    ("US100.cash", 1.0,      6.5,  "D1"),
    ("US500.cash", 1.0,      5.0,  "D1"),
    ("JP225.cash", 1.0,      2.0,  "D1"),
    ("GER40.cash", 1.0,      3.0,  "D1"),
    ("UK100.cash", 1.0,      4.0,  "D1"),
    ("XAUUSD",     100.0,    0.30, "H1"),
    ("XPTUSD",     100.0,    0.25, "H1"),
    ("US100.cash", 1.0,      6.5,  "H1"),
    ("HK50.cash",  1.0,      3.0,  "H1"),
]


def _eval(df, sigs, mpu, ticker, sname):
    sym = try_load(ticker)
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
        pf_train=pf_tr, pf_test=pf_te,
        r_test=test.avg_R, wr_test=test.win_rate,
        oos_dd_pct=oos_dd, recov_oos=rec,
        net_pnl=res.sum_realized_pnl,
        max_consec_losses=max_streak_loss,
        deploy_safe=(len(gates) == 0),
        gates_failed=gates,
    )


PAIRS = [
    # (long_strat, short_strat, label_long, label_short)
    (lambda rr: LastWeekLow(LastWeekLowParams(rr_target=rr)),
     lambda rr: LastWeekHigh(LastWeekHighParams(rr_target=rr)),
     "last_week_low", "last_week_high"),
    (lambda rr: SupportResistance(SupportResistanceParams(
                       variant="plain", rr_target=rr)),
     lambda rr: SupportResistanceShort(SupportResistanceShortParams(
                       variant="plain", rr_target=rr)),
     "sr_long", "sr_short"),
    (lambda rr: EmaSpreadPullback(EmaSpreadPullbackParams(rr_target=rr)),
     lambda rr: EmaSpreadPullbackShort(EmaSpreadPullbackShortParams(
                       rr_target=rr)),
     "ema_spread_long", "ema_spread_short"),
]
RR_TARGETS = [2.0]   # focus on RR 1:2 for direct comparison


def main():
    rows: list[dict] = []
    for ticker, mpu, _lots, tf in TICKERS:
        ppath = ROOT / "data" / f"{ticker}_{tf}.parquet"
        if not ppath.exists():
            continue
        df = load_parquet(ppath)
        if len(df) < 250:
            continue
        for long_fac, short_fac, lbl_l, lbl_s in PAIRS:
            for rr in RR_TARGETS:
                for fac, lbl in ((long_fac, lbl_l), (short_fac, lbl_s)):
                    try:
                        sigs = fac(rr).signals(df)
                        m = _eval(df, sigs, mpu, ticker, lbl)
                    except Exception as e:
                        m = None
                    if m is None:
                        continue
                    rows.append(dict(
                        ticker=ticker, tf=tf,
                        strategy=lbl, side=("long" if "long" in lbl
                                              or "low" in lbl else "short"),
                        rr=rr, **m,
                    ))

    # Print bull-vs-bear comparison per ticker × strategy pair
    print("=" * 120)
    print("BULLISH vs BEARISH — same ticker, same R:R, OOS-validated")
    print("=" * 120)
    print(f"{'Ticker':12} {'TF':4} {'Pair':18} {'Side':6} "
          f"{'n':>4} {'PF':>5} {'R':>6} {'DD%':>5} {'recov':>7} "
          f"{'maxLoss':>7} {'gate':>5}")
    print("-" * 120)
    by_pair = {}
    for r in rows:
        pair_key = (r["ticker"], r["tf"], r["strategy"].rsplit("_", 1)[0])
        by_pair.setdefault(pair_key, []).append(r)

    survivors = []
    for (tic, tf, basekey), pair_rows in by_pair.items():
        for r in sorted(pair_rows, key=lambda x: x["side"]):
            tag = "✅" if r["deploy_safe"] else "🚫"
            rec_s = (f"{r['recov_oos']:.0f}d"
                      if r['recov_oos'] is not None else "?")
            print(f"{r['ticker']:12} {r['tf']:4} {basekey:18} {r['side']:6} "
                  f"{r['n_test']:>4} {r['pf_test']:>5.2f} "
                  f"{r['r_test']:>+6.2f} {r['oos_dd_pct']:>4.1f}% "
                  f"{rec_s:>7} {r['max_consec_losses']:>7} {tag:>5}")
            if r["deploy_safe"] and r["pf_test"] >= 1.3:
                survivors.append(r)
        print()

    print("=" * 120)
    print(f"OOS-VALIDATED BEARISH SURVIVORS (PF>=1.3): "
          f"{sum(1 for s in survivors if s['side']=='short')}")
    print("=" * 120)
    for r in [s for s in survivors if s['side'] == 'short']:
        rec_s = (f"{r['recov_oos']:.0f}d"
                  if r['recov_oos'] is not None else "?")
        print(f"  ✅ {r['strategy']:24} {r['ticker']:12} {r['tf']:4}  "
              f"PF={r['pf_test']:.2f}  R={r['r_test']:+.2f}  "
              f"DD={r['oos_dd_pct']:.1f}%  recov={rec_s}  "
              f"net=${r['net_pnl']:+,.0f}")


if __name__ == "__main__":
    main()
