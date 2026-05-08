#!/usr/bin/env python3
"""seed_tier1_v2db.py — populate v2.db with the 5 Tier 1 cells so the
weekly rebaseline LaunchAgent picks them up on its next run.

Each cell is run through `core.backtest.run_backtest` at standard cost
defaults and the result is persisted via `core.storage.save_run`.

After this seeds v2.db, `make rebaseline` (or the weekly LaunchAgent)
will see the new cells in `_list_unique_cells` and re-baseline them
alongside everything else.
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

from core import cost_defaults, storage   # noqa: E402
from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402

from strategies.last_week_low import (   # noqa: E402
    LastWeekLow, LastWeekLowParams,
)
from strategies.ema_spread_pullback import (   # noqa: E402
    EmaSpreadPullback, EmaSpreadPullbackParams,
)


CELLS = [
    ("XAUUSD", "D1", 100.0,    0.30, "ema_spread_pullback",
     EmaSpreadPullbackParams(rr_target=3.0)),
    ("XPDUSD", "D1", 100.0,    0.20, "ema_spread_pullback",
     EmaSpreadPullbackParams(rr_target=3.0)),
    ("XPTUSD", "D1", 100.0,    0.25, "ema_spread_pullback",
     EmaSpreadPullbackParams(rr_target=3.0)),
    ("JP225.cash", "D1", 1.0,  2.0,  "last_week_low",
     LastWeekLowParams(rr_target=2.0)),
    ("US500.cash", "D1", 1.0,  5.0,  "last_week_low",
     LastWeekLowParams(rr_target=4.0)),
]


def _build_strategy(name: str, params):
    if name == "last_week_low":
        return LastWeekLow(params)
    if name == "ema_spread_pullback":
        return EmaSpreadPullback(params)
    raise ValueError(name)


def main() -> int:
    db_path = ROOT / "data" / "v2.db"
    storage.init_schema(db_path)
    print(f"Seeding v2.db at {db_path}")
    print(f"Costs: ${cost_defaults.DEFAULT_COMMISSION_USD} commission, "
          f"{cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC} ATR slip\n")

    ok = 0
    for sym, tf, mpu, lots, sname, params in CELLS:
        ppath = ROOT / "data" / f"{sym}_{tf}.parquet"
        if not ppath.exists():
            print(f"  ⚠ skip {sym} {tf} {sname}: parquet missing")
            continue
        df = load_parquet(ppath)
        strat = _build_strategy(sname, params)
        sigs = strat.signals(df)
        run_id = uuid.uuid4().hex[:12]
        cfg = {f: getattr(params, f)
               for f in params.__dataclass_fields__.keys()}
        try:
            res = run_backtest(
                df, sigs, starting_balance=100_000.0, lots=lots,
                money_per_unit_price=mpu,
                commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
                slippage_per_fill_atr_frac=(
                    cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC),
                symbol=sym,
            )
            n = len(res.trades)
            pnl = sum(t.realized_pnl for t in res.trades)
            now_iso = datetime.now(timezone.utc).isoformat()
            storage.save_run(
                db_path, run_id,
                started_at_utc=now_iso,
                symbol=sym, tf=tf,
                strategy_name=sname,
                config_json=json.dumps(cfg, sort_keys=True),
                starting_balance=100_000.0,
            )
            # Trades → dicts
            trade_dicts = []
            for t in res.trades:
                t_dict = {
                    "symbol": sym,
                    "direction": t.direction,
                    "opened_at_utc": str(df["time"].iloc[t.entry_bar_idx]),
                    "closed_at_utc": str(df["time"].iloc[t.exit_bar_idx]),
                    "entry_price": t.entry_price,
                    "stop_price": t.stop_price,
                    "target_price": t.target_price,
                    "exit_price": t.exit_price,
                    "lots": t.lots,
                    "realized_pnl": t.realized_pnl,
                    "r_multiple": t.r_multiple,
                    "close_reason": str(t.close_reason),
                    "mode": "backtest",
                    "strategy": sname,
                    "tf": tf,
                }
                trade_dicts.append(t_dict)
            storage.save_trades(db_path, run_id, trade_dicts)
            storage.finish_run(
                db_path, run_id,
                finished_at_utc=now_iso,
                ending_equity=res.ending_balance,
                n_trades=n,
                sum_realized_pnl=res.sum_realized_pnl,
                equity_curve_pnl=res.equity_curve_pnl,
                reconciles=res.reconciles,
            )
            print(f"  ✅ {sym:10} {tf} {sname:22}  n={n:>3}  "
                  f"pnl=${pnl:+8,.0f}  run_id={run_id}")
            ok += 1
        except Exception as e:
            print(f"  ❌ {sym} {tf} {sname}: {e!r}")

    print(f"\nSeeded {ok}/{len(CELLS)} cells.")
    print("Now `make rebaseline` (or the weekly LaunchAgent) will "
          "include these cells.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
