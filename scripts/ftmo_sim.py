#!/usr/bin/env python3
"""
ftmo_sim.py — run the FTMO pass-rate Monte-Carlo simulator on the configured
portfolio and print a summary table.

By default uses a hard-coded 'survivor' portfolio (vol_breakout on US100/GER40/
USDJPY D1) with synthetic OOS R distributions derived from a quick backtest.
Pass --json <path> to load a custom portfolio.

Usage:
    make ftmo-sim
    make ftmo-sim ARGS="--iterations 50000"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.ftmo_simulator import StrategyDist, simulate_pass_rate   # noqa: E402
from strategies.vol_breakout import VolBreakout, VolBreakoutParams   # noqa: E402


SURVIVOR_PORTFOLIO = [
    # Documented in docs/index_edge_findings.md
    {"strategy": "vol_breakout", "symbol": "US100.cash", "tf": "D1",
      "long_only": True, "trades_per_day": 0.18, "risk_pct": 1.0},
    {"strategy": "vol_breakout", "symbol": "GER40.cash", "tf": "D1",
      "long_only": True, "trades_per_day": 0.20, "risk_pct": 1.0},
    {"strategy": "vol_breakout", "symbol": "USDJPY", "tf": "D1",
      "long_only": True, "trades_per_day": 0.18, "risk_pct": 1.0},
]
DEFAULT_MPU = {
    "US100.cash": 1.0, "GER40.cash": 1.0, "USDJPY": 700.0,
    "EURUSD": 100_000.0, "EU50.cash": 1.10,
}
DEFAULT_LOTS = {
    "US100.cash": 6.5, "GER40.cash": 3.0, "USDJPY": 1.0,
    "EURUSD": 1.0, "EU50.cash": 20.0,
}


def _bootstrap_oos_R(symbol: str, tf: str, long_only: bool) -> np.ndarray:
    """Run a quick backtest on cached parquet, partition train/test, return
    test-side R-multiples. Used as the bootstrap pool for the simulator."""
    path = REPO / "data" / f"{symbol}_{tf}.parquet"
    if not path.exists():
        return np.array([])
    df = load_parquet(path)
    strat = VolBreakout(VolBreakoutParams(long_only=long_only))
    r = run_backtest(
        df, strat.signals(df),
        starting_balance=100_000,
        lots=DEFAULT_LOTS.get(symbol, 0.1),
        money_per_unit_price=DEFAULT_MPU.get(symbol, 1.0),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        symbol=symbol,
        enforce_weekend_flat=True, enforce_daily_flat=True,
    )
    train, test = partition_train_test(r, 0.6, n_bars=len(df))
    test_trades = [t for t in r.trades
                    if t.entry_bar_idx >= int(len(df) * 0.6)]
    return np.array([t.r_multiple for t in test_trades], dtype=float)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None,
                     help="path to a JSON portfolio file (overrides defaults)")
    ap.add_argument("--iterations", type=int, default=10_000)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--starting-balance", type=float, default=100_000)
    ap.add_argument("--daily-cap", type=float, default=5.0,
                     help="daily-loss cap %% (FTMO Phase 1 default)")
    ap.add_argument("--total-cap", type=float, default=10.0)
    ap.add_argument("--target", type=float, default=10.0,
                     help="pass-target %% above starting balance")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.json:
        portfolio_spec = json.loads(Path(args.json).read_text())
    else:
        portfolio_spec = SURVIVOR_PORTFOLIO
        print(f"[ftmo-sim] using built-in survivor portfolio "
              f"({len(SURVIVOR_PORTFOLIO)} slots)")

    pool = []
    for spec in portfolio_spec:
        rs = _bootstrap_oos_R(spec["symbol"], spec["tf"],
                                spec.get("long_only", True))
        if rs.size == 0:
            print(f"  [warn] no OOS data for {spec['symbol']} {spec['tf']}")
            continue
        print(f"  [{spec['strategy']} on {spec['symbol']} {spec['tf']}] "
              f"OOS pool n={len(rs)} mean_R={rs.mean():+.3f}")
        pool.append(StrategyDist(
            name=f"{spec['strategy']}_{spec['symbol']}_{spec['tf']}",
            symbol=spec["symbol"],
            r_multiples=rs,
            trades_per_day=spec.get("trades_per_day", 0.5),
            risk_per_trade_pct=spec.get("risk_pct", 1.0),
        ))

    if not pool:
        raise SystemExit("portfolio is empty — nothing to simulate")

    print(f"\n[ftmo-sim] running {args.iterations:,} iterations × "
          f"{args.days} days...")
    res = simulate_pass_rate(
        pool, starting_balance=args.starting_balance,
        days=args.days,
        daily_loss_cap_pct=args.daily_cap,
        total_loss_cap_pct=args.total_cap,
        pass_target_pct=args.target,
        n_iterations=args.iterations,
        seed=args.seed,
    )

    print()
    print("┌─────────────────────────────────────────────┐")
    print(f"│  FTMO Phase 1 pass probability: **{res.p_pass*100:.1f}%**            │")
    print("└─────────────────────────────────────────────┘")
    print(f"  P(pass in {args.days} days)        = {res.p_pass*100:6.2f}%")
    print(f"  P(daily breach)            = {res.p_daily_breach*100:6.2f}%")
    print(f"  P(total breach)            = {res.p_total_breach*100:6.2f}%")
    print(f"  P(no resolution by horizon)= {res.p_no_resolution*100:6.2f}%")
    print(f"  Expected final return      = {res.expected_final_pct:+.2f}%")
    print(f"  Median final return        = {res.median_final_pct:+.2f}%")
    print()
    print("Per-strategy contribution to expected return:")
    for name, c in sorted(res.contrib_R_per_strategy.items(),
                            key=lambda x: -x[1]):
        dd = res.contrib_dd_per_strategy.get(name, 0)
        print(f"  {name:55s}  ret={c:+6.2f}%  dd={dd:+6.2f}%")


if __name__ == "__main__":
    main()
