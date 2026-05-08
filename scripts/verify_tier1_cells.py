#!/usr/bin/env python3
"""Verify the new Tier 1 strategy modules produce stats matching the
sweeps. Runs each cell through core.backtest.run_backtest with the
same cost defaults used by the catalog rebaseline.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from core import cost_defaults
from core.data import load_parquet
from core.backtest import run_backtest

from strategies.last_week_low import LastWeekLow, LastWeekLowParams
from strategies.ema_spread_pullback import (
    EmaSpreadPullback, EmaSpreadPullbackParams,
)
from strategies.vwap_first_touch import (
    VwapFirstTouch, VwapFirstTouchParams,
)


CELLS = [
    ("XAUUSD",     "D1", 100.0,    0.30, "ema_spread_pullback", "RR_1_3",
     EmaSpreadPullback(EmaSpreadPullbackParams(rr_target=3.0))),
    ("XPDUSD",     "D1", 100.0,    0.20, "ema_spread_pullback", "RR_1_3",
     EmaSpreadPullback(EmaSpreadPullbackParams(rr_target=3.0))),
    ("XPTUSD",     "D1", 100.0,    0.25, "ema_spread_pullback", "RR_1_3",
     EmaSpreadPullback(EmaSpreadPullbackParams(rr_target=3.0))),
    ("XAGUSD",     "D1", 5_000.0,  0.40, "ema_spread_pullback", "RR_1_3",
     EmaSpreadPullback(EmaSpreadPullbackParams(rr_target=3.0))),
    ("JP225.cash", "D1", 1.0,      2.0,  "last_week_low",       "RR_1_2",
     LastWeekLow(LastWeekLowParams(rr_target=2.0))),
    ("US500.cash", "D1", 1.0,      5.0,  "last_week_low",       "RR_1_4",
     LastWeekLow(LastWeekLowParams(rr_target=4.0))),
    ("US100.cash", "H1", 1.0,      6.5,  "vwap_first_touch",    "RR_1_2",
     VwapFirstTouch(VwapFirstTouchParams(rr_target=2.0))),
    ("XPTUSD",     "H1", 100.0,    0.25, "vwap_first_touch",    "RR_1_2",
     VwapFirstTouch(VwapFirstTouchParams(rr_target=2.0))),
]


def main():
    print(f"{'Ticker':>10} {'TF':>3} {'Strategy':22} {'Var':>7} "
          f"{'n':>4} {'PF':>5} {'WR%':>4} {'meanR':>6} "
          f"{'net':>10} {'DD%':>5}")
    print("-" * 88)
    for tic, tf, mpu, lots, sname, vname, strat in CELLS:
        df = load_parquet(ROOT / "data" / f"{tic}_{tf}.parquet")
        sigs = strat.signals(df)
        res = run_backtest(
            df, sigs, starting_balance=100_000, lots=lots,
            money_per_unit_price=mpu,
            commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
            slippage_per_fill_atr_frac=cost_defaults
            .DEFAULT_SLIPPAGE_ATR_FRAC,
        )
        trades = res.trades
        if not trades:
            print(f"{tic:>10} {tf:>3} {sname:22} {vname:>7}    0   "
                  f"no-trades")
            continue
        pnls = [t.realized_pnl for t in trades]
        rs = [t.r_multiple for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gp = sum(wins)
        gl = -sum(losses)
        pf = gp / gl if gl > 0 else 9.99
        wr = len(wins) / n * 100
        eq = pd.Series([0.0] + [sum(pnls[:i + 1]) for i in range(n)])
        dd = float((eq.cummax() - eq).max())
        mean_r = sum(rs) / len(rs) if rs else 0
        net = sum(pnls)
        print(f"{tic:>10} {tf:>3} {sname:22} {vname:>7} "
              f"{n:>4} {pf:>5.2f} {int(round(wr)):>3}% "
              f"{mean_r:>+5.2f} ${net:>+9,.0f} {dd / 1000:>4.1f}%")


if __name__ == "__main__":
    main()
