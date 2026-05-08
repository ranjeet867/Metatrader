#!/usr/bin/env python3
"""seed_hk50_m15_us500_m15.py — additional 2 paper deployments after
deep-dive analysis of sweep runners-up.

Skipped: US100 D1 donchian_20 (only 6 trades/year — can't validate edge)
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

from strategies.donchian_breakout import (   # noqa: E402
    DonchianBreakout, DonchianBreakoutParams,
)
from strategies.ema_cross import EmaCross, EmaCrossParams   # noqa: E402


CELLS = [
    ("ema_cross_12_26", "HK50.cash", "M15", 1.0,
     EmaCross,
     EmaCrossParams(
         fast_period=12, slow_period=26, atr_period=14,
         stop_atr_mult=1.5, target_atr_mult=3.0,
         long_only=True, regime_ema_period=200,
     ),
     "ema_cross_12_26 HK50 M15 RR_1:2 long + 200-EMA. OOS PF 1.92, "
     "R +0.51, recov 6d. Train/test perfectly aligned (PF 1.86/1.92). "
     "Highest trade frequency (114/year). 13% DD at 0.30%, sized to "
     "0.05% → ~2.2% expected DD = FTMO-safe."),

    ("donchian_20", "US500.cash", "M15", 1.0,
     DonchianBreakout,
     DonchianBreakoutParams(
         period=20, atr_period=14,
         stop_atr_mult=1.5, target_atr_mult=3.0,
         long_only=True, regime_ema_period=200,
     ),
     "donchian_20 US500 M15 RR_1:2 long + 200-EMA. OOS PF 1.56, "
     "R +0.31, DD 3.3%, recov 8d. n_test=74 (strong sample). 474 "
     "trades/year. Different ticker from existing US100 cells = "
     "real diversification."),
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
    print("Seeding 2 runner-up sweep cells as PAPER\n")
    for sname, ticker, tf, mpu, factory, params, notes in CELLS:
        _seed_one(sname, ticker, tf, mpu, factory, params, notes)
        print()
    print("DONE. 2 new paper cells live on next runner tick.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
