#!/usr/bin/env python3
"""sweep_existing_winners_variants.py — find improved variants of cells
already in the deploy-safe catalog.

For each "winner" cell we try a handful of (stop_atr_mult, R:R) tweaks
plus long-only vs bidir to see if a small param change unlocks more
edge. Variants that PASS the deploy-safe gate AND beat the original on
either PF, mean_R, or recovery are promoted to a "candidates" report.

Cells tested (top of current deploy-safe list):
  - rsi_30_70 EURUSD M15 long
  - rsi_30_70 XPDUSD H1 long
  - donchian_55 XAUUSD H1 1:1.1
  - ema_cross_9_20 US100 M15 1:2 weekly
  - ema_cross_12_26 HK50 H1 1:2
  - donchian_55 US100 D1 1:1.5
  - last_week_low JP225 D1 1:2
  - donchian_20 US100 M15 1:1.5

Saves to data/experiment_log.json under strategy="winner_variants".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import cost_defaults, experiment_log
from core.backtest import run_backtest, partition_train_test
from core.backtest_stats import compute_full_stats
from core.data import load_parquet
from core.symbol_info_loader import try_load


# ---------------------------------------------------------------------------
# Cells to optimize. Each is (label, ticker, tf, strategy_factory, mpu).
# strategy_factory(stop_mult, target_mult, long_only) → (Strategy, Params)
# ---------------------------------------------------------------------------

def _ema_cross(stop, target, long_only, fast=9, slow=20):
    from strategies.ema_cross import EmaCross, EmaCrossParams
    p = EmaCrossParams(fast_period=fast, slow_period=slow,
                        atr_period=14, stop_atr_mult=stop,
                        target_atr_mult=target, long_only=long_only)
    return EmaCross(p), {"fast": fast, "slow": slow,
                          "stop_atr_mult": stop,
                          "target_atr_mult": target,
                          "long_only": long_only}


def _donchian(stop, target, long_only, period=20):
    from strategies.donchian_breakout import (
        DonchianBreakout, DonchianBreakoutParams,
    )
    p = DonchianBreakoutParams(period=period, atr_period=14,
                                 stop_atr_mult=stop,
                                 target_atr_mult=target,
                                 long_only=long_only)
    return DonchianBreakout(p), {"period": period,
                                  "stop_atr_mult": stop,
                                  "target_atr_mult": target,
                                  "long_only": long_only}


def _rsi(stop, target, long_only, period=14, ob=70, os=30):
    from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
    p = RsiMeanRevParams(rsi_period=period, oversold=os,
                          overbought=ob, atr_period=14,
                          stop_atr_mult=stop, target_atr_mult=target,
                          long_only=long_only)
    return RsiMeanRev(p), {"rsi_period": period,
                            "oversold": os, "overbought": ob,
                            "stop_atr_mult": stop,
                            "target_atr_mult": target,
                            "long_only": long_only}


CELLS = [
    # (label, ticker, tf, factory, mpu, factory_kwargs)
    ("rsi EURUSD M15",   "EURUSD",     "M15", _rsi,     1.0,
     {"period": 14, "ob": 70, "os": 30}),
    ("rsi XPDUSD H1",    "XPDUSD",     "H1",  _rsi,     100.0,
     {"period": 14, "ob": 70, "os": 30}),
    ("donch55 XAUUSD H1","XAUUSD",     "H1",  _donchian, 100.0,
     {"period": 55}),
    ("ema9_20 US100 M15","US100.cash", "M15", _ema_cross, 1.0,
     {"fast": 9, "slow": 20}),
    ("ema12_26 HK50 H1", "HK50.cash",  "H1",  _ema_cross, 1.0,
     {"fast": 12, "slow": 26}),
    ("donch55 US100 D1", "US100.cash", "D1",  _donchian, 1.0,
     {"period": 55}),
    ("donch20 US100 M15","US100.cash", "M15", _donchian, 1.0,
     {"period": 20}),
    ("ema12_26 HK50 M15","HK50.cash",  "M15", _ema_cross, 1.0,
     {"fast": 12, "slow": 26}),
]

# Variants per cell — each (label, stop_atr, target_atr, long_only)
VARIANTS = [
    ("RR_1:1_long",  1.5, 1.5,  True),
    ("RR_1:2_long",  1.5, 3.0,  True),
    ("RR_1:3_long",  1.5, 4.5,  True),
    ("RR_1:2_bidir", 1.5, 3.0,  False),
    ("RR_1:1_tight", 1.0, 1.0,  True),
    ("RR_1:2_wide",  2.0, 4.0,  True),
]


def _run(ticker, tf, factory, mpu, factory_kw, stop, target, long_only):
    df = load_parquet(ROOT / "data" / f"{ticker}_{tf}.parquet")
    sym = try_load(ticker)
    strat, cfg_dict = factory(stop, target, long_only, **factory_kw)
    sigs = strat.signals(df)
    res = run_backtest(
        df, sigs, starting_balance=100_000.0, lots=0.0,
        money_per_unit_price=mpu,
        commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        risk_pct=0.30, symbol_info=sym,
    )
    train, test = partition_train_test(res, train_pct=0.6, n_bars=len(df))
    stats = compute_full_stats(res, starting_balance=100_000.0)
    pf_te = (99.99 if test.profit_factor == float("inf")
              else test.profit_factor)
    pf_tr = (99.99 if train.profit_factor == float("inf")
              else train.profit_factor)
    rec = stats.recovery_duration_days
    gate_ok = (pf_te >= 1.0 and test.avg_R >= 0
               and test.n_trades >= 15
               and rec is not None and rec <= 180)
    return dict(
        n_trades=len(res.trades),
        n_test=test.n_trades,
        pf_train=round(pf_tr, 3),
        pf_test=round(pf_te, 3),
        r_test=round(test.avg_R, 3),
        wr_test=round(test.win_rate, 1),
        max_dd_pct=round(stats.max_dd_pct, 2),
        recov_days=(round(rec, 1) if rec is not None else None),
        net_pnl=round(res.sum_realized_pnl, 0),
        gate_ok=gate_ok,
        config=cfg_dict,
    )


def main():
    log = experiment_log.load()
    print(f"Loaded experiment log: {len(log.records)} prior records\n")

    results: list[dict] = []
    for label, ticker, tf, factory, mpu, factory_kw in CELLS:
        print(f"=== {label} ({ticker} {tf}) ===")
        for vname, stop, target, lo in VARIANTS:
            try:
                r = _run(ticker, tf, factory, mpu, factory_kw,
                          stop, target, lo)
                tag = "✅" if r["gate_ok"] else "🚫"
                print(f"  {vname:14}  n={r['n_test']:>3}  "
                      f"PF_te={r['pf_test']:>5.2f}  R={r['r_test']:>+5.2f}  "
                      f"DD={r['max_dd_pct']:>5.1f}%  "
                      f"recov={(str(r['recov_days'])+'d' if r['recov_days'] else '?'):>6}  "
                      f"net=${r['net_pnl']:>+8,.0f}  {tag}")
                rec = dict(label=label, ticker=ticker, tf=tf,
                            variant=vname, **r)
                results.append(rec)
                # Persist to experiment log
                log.record(
                    strategy="winner_variants", variant=f"{label}_{vname}",
                    ticker=ticker, tf=tf,
                    stats={"n": r["n_trades"], "pf": r["pf_test"],
                           "wr": r["wr_test"],
                           "mean_R": r["r_test"],
                           "gain": r["net_pnl"],
                           "max_dd_pct": r["max_dd_pct"]},
                    params=r["config"],
                )
            except Exception as e:
                print(f"  {vname:14}  ERROR: {e!r}")
        print()
    log.save()

    # Promotion candidates: gate_ok AND (PF > 1.5 OR mean_R > +0.30)
    promotion = [r for r in results
                 if r["gate_ok"]
                 and (r["pf_test"] > 1.5 or r["r_test"] > 0.30)]
    promotion.sort(key=lambda x: -(x["pf_test"] * max(0.001, x["r_test"])))

    print("=" * 100)
    print(f"PROMOTION CANDIDATES — gate-pass + strong edge ({len(promotion)})")
    print("=" * 100)
    print(f"{'Cell':28} {'Variant':14} {'n':>4} {'PF':>5} "
          f"{'R':>6} {'DD%':>5} {'recov':>6} {'net$':>10}")
    for r in promotion[:25]:
        rec_s = (f"{r['recov_days']:.0f}d"
                  if r['recov_days'] is not None else "?")
        print(f"{r['label'][:28]:28} {r['variant']:14} {r['n_test']:>4} "
              f"{r['pf_test']:>5.2f} {r['r_test']:>+6.2f} "
              f"{r['max_dd_pct']:>4.1f}% {rec_s:>6} "
              f"${r['net_pnl']:>+8,.0f}")


if __name__ == "__main__":
    main()
