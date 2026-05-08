#!/usr/bin/env python3
"""seed_double_bottom_winners.py — ship the top 2 double_bottom cells
from the 2026-05-08 new-strategy sweep as PAPER deployments at 0.05%
risk + $50 max-$. 4-week observation window, then promote to live if
live R holds catalog edge.

Top 2:
  1. US100.cash M15 double_bottom RR 1:3 long (no RSI div)
     OOS PF 3.98, R +1.21, WR 60%, DD 3.2%, recov 18d, n_test=15.
     Best raw edge in the new-strategy sweep.

  2. US500.cash M15 double_bottom RR 1:2 long (no RSI div)
     OOS PF 3.11, R +0.82, WR 62%, DD 2.3%, recov 16d, n_test=16.
     Best risk-adjusted profile: lowest DD, fastest recovery.

Conservative 0.05% per trade + $50 max-$ matches the same risk envelope
as your existing safe-deploy cells. With n_test = 15-16, the sample is
above the 15-trade hard gate but small in absolute terms — paper for
4 weeks before promoting.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import account_manager, cost_defaults, storage   # noqa: E402
from core import deployment as dep_mod   # noqa: E402
from core.backtest import run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.deployment import Deployment   # noqa: E402
from core.symbol_info_loader import try_load   # noqa: E402

from strategies.double_bottom import DoubleBottom, DoubleBottomParams   # noqa: E402


# (sname, ticker, tf, mpu, factory, params, notes)
CELLS = [
    ("double_bottom", "US100.cash", "M15", 1.0,
     DoubleBottom,
     DoubleBottomParams(
         pivot_window=5, min_separation=8, max_separation=60,
         band_pct=0.003, rsi_period=14,
         require_divergence=False,    # NODIV variant — best in sweep
         atr_period=14, stop_atr_mult=0.5,
         target_R_mult=3.0,           # RR 1:3 — winner
         max_wait_bars=20, long_only=True,
     ),
     "double_bottom US100 M15 RR_1:3 long. OOS PF 3.98, R +1.21, "
     "WR 60%, DD 3.2%, recov 18d, n_test=15. Best raw edge in "
     "2026-05-08 new-strategy sweep. Different signal kernel from "
     "donchian/ema/rsi cells = real diversification."),

    ("double_bottom", "US500.cash", "M15", 1.0,
     DoubleBottom,
     DoubleBottomParams(
         pivot_window=5, min_separation=8, max_separation=60,
         band_pct=0.003, rsi_period=14,
         require_divergence=False,
         atr_period=14, stop_atr_mult=0.5,
         target_R_mult=2.0,           # RR 1:2 — best risk-adjusted
         max_wait_bars=20, long_only=True,
     ),
     "double_bottom US500 M15 RR_1:2 long. OOS PF 3.11, R +0.82, "
     "WR 62%, DD 2.3%, recov 16d, n_test=16. Best risk-adjusted: "
     "lowest DD, fastest recovery. Different ticker from US100 cell "
     "above for diversification."),
]


def _seed_one(sname, ticker, tf, mpu, factory, params, notes):
    db_path = ROOT / "data" / "v2.db"
    storage.init_schema(db_path)
    df = load_parquet(ROOT / "data" / f"{ticker}_{tf}.parquet")
    sym_info = try_load(ticker)
    sigs = factory(params).signals(df)
    res = run_backtest(
        df, sigs, starting_balance=100_000.0, lots=0.0,
        money_per_unit_price=mpu,
        commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        risk_pct=0.30, symbol_info=sym_info, symbol=ticker,
    )
    n = len(res.trades)
    pnl = sum(t.realized_pnl for t in res.trades)
    run_id = uuid.uuid4().hex[:12]
    now_iso = datetime.now(timezone.utc).isoformat()
    cfg = {f: getattr(params, f) for f in params.__dataclass_fields__.keys()}
    storage.save_run(
        db_path, run_id, started_at_utc=now_iso,
        symbol=ticker, tf=tf, strategy_name=sname,
        config_json=json.dumps(cfg, sort_keys=True),
        starting_balance=100_000.0,
    )
    trade_dicts = [{
        "symbol": ticker, "direction": t.direction,
        "opened_at_utc": str(df["time"].iloc[t.entry_bar_idx]),
        "closed_at_utc": str(df["time"].iloc[t.exit_bar_idx]),
        "entry_price": t.entry_price, "stop_price": t.stop_price,
        "target_price": t.target_price, "exit_price": t.exit_price,
        "lots": t.lots, "realized_pnl": t.realized_pnl,
        "r_multiple": t.r_multiple, "close_reason": str(t.close_reason),
        "mode": "backtest", "strategy": sname, "tf": tf,
    } for t in res.trades]
    storage.save_trades(db_path, run_id, trade_dicts)
    storage.finish_run(
        db_path, run_id, finished_at_utc=now_iso,
        ending_equity=res.ending_balance,
        n_trades=n, sum_realized_pnl=res.sum_realized_pnl,
        equity_curve_pnl=res.equity_curve_pnl,
        reconciles=res.reconciles,
    )
    print(f"  ✅ v2.db {sname} {ticker} {tf}  n={n}  pnl=${pnl:+,.0f}")

    accounts = account_manager.list_accounts()
    if not accounts:
        raise RuntimeError("no MT5 account configured")
    active = accounts[0]
    deployment_id = Deployment.slug(sname, ticker, tf)
    dep = Deployment(
        deployment_id=deployment_id, strategy=sname,
        ticker=ticker, tf=tf, long_only=True, params=cfg,
        risk_pct=0.05, daily_cap_pct=0.5, max_money_risk_usd=50.0,
        max_lots=15.0, status="paper", notes=notes,
    )
    dep_mod.upsert_deployment(active.login, dep)
    print(f"     paper deployment: {deployment_id}")


def main() -> int:
    print("Seeding 2 new-strategy sweep winners as PAPER\n"
          "  - US100.cash M15 double_bottom RR_1:3 long\n"
          "  - US500.cash M15 double_bottom RR_1:2 long\n"
          "Risk: 0.05% per trade, $50 max-$ cap, 4-week observation.\n")
    for sname, ticker, tf, mpu, factory, params, notes in CELLS:
        _seed_one(sname, ticker, tf, mpu, factory, params, notes)
        print()
    print("DONE. 2 new paper cells live on next runner tick.")
    print("Watch live R for 4 weeks, then promote at 0.10% if green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
