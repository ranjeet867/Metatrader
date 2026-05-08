"""
rebaseline_catalog.py — Re-run every catalog cell at the standard
cost-priced configuration so v2.db contains a self-consistent set
of metrics. After running this, every dashboard view (Composer,
Library, Backtest page, Compare) shows matching numbers for the
same cell, because they all read the SAME v2.db rows produced under
the SAME cost assumptions.

Usage:
    # Dry-run — show what WOULD be re-run, no DB writes:
    python scripts/rebaseline_catalog.py --dry-run

    # Re-run only currently-deploy_safe cells (fast, ~1 min):
    python scripts/rebaseline_catalog.py --safe-only

    # Re-run EVERY cell in the catalog (~15 min on M1):
    python scripts/rebaseline_catalog.py

Costs are sourced from core.cost_defaults — same constants the
Backtest page + sweep + run_backtest CLI use. To change the cost
model: edit cost_defaults.py, then re-run this script.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import cost_defaults, edge_catalog, storage   # noqa: E402
from core.backtest import run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.symbol_info_loader import try_load as try_load_symbol_info   # noqa: E402
from dashboards.components.state import (   # noqa: E402
    discover_strategies, resolve_money_per_unit,
)


def _decode_variant(strategy_name: str, config_json: str
                     ) -> tuple[str, dict] | None:
    """Map a v2.db (strategy_name, config_json) row back to a base
    strategy name + a config dict that can be passed to the
    appropriate Params dataclass."""
    try:
        cfg = json.loads(config_json) if config_json else {}
    except json.JSONDecodeError:
        return None
    return strategy_name, cfg


def _list_unique_cells(db_path: Path
                        ) -> list[tuple[str, str, str, str]]:
    """Return [(symbol, tf, strategy_name, config_json), ...] for every
    UNIQUE (symbol, tf, strategy_name, config_json) combo in v2.db.
    The latest run for each combo wins on subsequent dashboard reads
    (edge_catalog._load_from_v2db sorts by started_at_utc DESC), so
    re-running once with consistent costs is enough — older rows
    become invisible to the catalog."""
    if not db_path.exists():
        return []
    seen: set[tuple] = set()
    out: list[tuple[str, str, str, str]] = []
    with sqlite3.connect(str(db_path)) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("""
            SELECT symbol, tf, strategy_name, config_json
            FROM runs
            WHERE n_trades IS NOT NULL AND n_trades > 0
              AND reconciles = 1
            ORDER BY started_at_utc DESC
        """).fetchall()
        for r in rows:
            k = (r["symbol"], r["tf"], r["strategy_name"], r["config_json"])
            if k in seen:
                continue
            seen.add(k)
            out.append(k)
    return out


def _run_one(symbol: str, tf: str, strategy_name: str, config_json: str,
              *, db_path: Path, strategies: dict) -> dict:
    """Re-run one cell at standard cost. Returns a result summary."""
    parquet = ROOT / "data" / f"{symbol}_{tf}.parquet"
    if not parquet.exists():
        return {"status": "skipped", "reason": f"parquet missing: {parquet.name}"}

    # Resolve via the SHARED variant→base resolver — pre-fix this did a
    # naive `if strategy_name not in strategies` check, which silently
    # skipped any v2.db rows where strategy_name was a variant
    # (e.g. 'rsi_30_70') instead of the base ('rsi_meanrev'). Old runs
    # from before the resolver fix would never get re-baselined.
    from dashboards.components.strategy_resolver import resolve_base_strategy
    base = resolve_base_strategy(strategy_name, strategies) or strategy_name
    if base not in strategies:
        return {"status": "skipped",
                "reason": f"strategy `{strategy_name}` (base `{base}`) "
                          f"not found (renamed/removed?)"}

    decoded = _decode_variant(strategy_name, config_json)
    if decoded is None:
        return {"status": "skipped", "reason": "config_json unparseable"}
    _, cfg = decoded
    # Use the resolved base for class lookup, not the raw stored name.
    StratCls, ParamsCls = strategies[base]
    try:
        if ParamsCls is None:
            strategy = StratCls()
            params_obj = None
        else:
            field_names = {f.name for f in dataclasses.fields(ParamsCls)}
            kwargs = {k: v for k, v in cfg.items() if k in field_names}
            params_obj = ParamsCls(**kwargs)
            strategy = StratCls(params_obj)
    except Exception as e:
        return {"status": "error", "reason": f"strategy init: {e}"}

    df = load_parquet(parquet)
    sym_info = try_load_symbol_info(symbol)
    mpu = resolve_money_per_unit(symbol)

    try:
        signals = strategy.signals(df)
        result = run_backtest(
            df, signals,
            starting_balance=cost_defaults.DEFAULT_STARTING_BALANCE_USD,
            lots=0.1,
            money_per_unit_price=mpu,
            commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
            slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
            symbol=symbol,
            risk_pct=cost_defaults.DEFAULT_RISK_PCT,
            symbol_info=sym_info,
        )
    except Exception as e:
        return {"status": "error", "reason": f"backtest: {e}"}

    if not result.reconciles:
        return {"status": "error",
                "reason": f"reconcile fail: div=${abs(result.sum_realized_pnl - result.equity_curve_pnl):.4f}"}

    # Persist — 12 hex chars (48-bit) entropy to avoid collision under
    # parallel rebaseline. Pre-fix used [:6] (24-bit) which could collide.
    run_id = ("rebase_"
              + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
              + "_" + uuid.uuid4().hex[:12])
    storage.save_run(
        db_path, run_id,
        started_at_utc=datetime.now(timezone.utc).isoformat(),
        symbol=symbol, tf=tf, strategy_name=strategy.name,
        config_json=json.dumps(
            dataclasses.asdict(params_obj) if params_obj is not None else {}
        ),
        starting_balance=cost_defaults.DEFAULT_STARTING_BALANCE_USD,
    )
    trade_rows = [{
        "symbol": symbol, "direction": t.direction,
        "opened_at_utc": str(df["time"].iloc[t.entry_bar_idx]),
        "closed_at_utc": str(df["time"].iloc[t.exit_bar_idx]),
        "entry_price": t.entry_price, "stop_price": t.stop_price,
        "target_price": t.target_price, "exit_price": t.exit_price,
        "lots": t.lots, "realized_pnl": t.realized_pnl,
        "r_multiple": t.r_multiple, "close_reason": t.close_reason,
    } for t in result.trades]
    storage.save_trades(db_path, run_id, trade_rows)
    storage.finish_run(
        db_path, run_id,
        finished_at_utc=datetime.now(timezone.utc).isoformat(),
        ending_equity=result.ending_balance, n_trades=result.n_trades,
        sum_realized_pnl=result.sum_realized_pnl,
        equity_curve_pnl=result.equity_curve_pnl,
        reconciles=result.reconciles,
    )
    return {
        "status": "ok",
        "n": result.n_trades,
        "pnl": round(result.sum_realized_pnl, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "v2.db"))
    ap.add_argument("--dry-run", action="store_true",
                     help="Print what would be re-run, write nothing.")
    ap.add_argument("--safe-only", action="store_true",
                     help="Re-run only cells currently deploy_safe "
                           "(passing all hard gates) — fast, ~1 min. "
                           "Use this after a cost_defaults change to "
                           "refresh just the candidates that matter.")
    ap.add_argument("--limit", type=int, default=0,
                     help="Stop after N cells (0 = all). For testing.")
    args = ap.parse_args()

    db_path = Path(args.db)
    storage.init_schema(db_path)

    print(f"\n{'=' * 70}")
    print(f"  REBASELINE CATALOG — {cost_defaults.cost_config_badge()}")
    print(f"  DB: {db_path}")
    print(f"{'=' * 70}\n")

    cells = _list_unique_cells(db_path)
    print(f"Found {len(cells)} unique cells in v2.db.")

    if args.safe_only:
        cat = edge_catalog.load_catalog(db_path=db_path)
        safe_keys: set[tuple] = set()
        for rows in cat.values():
            for e in rows:
                if e.deploy_safe:
                    safe_keys.add((e.ticker, e.tf, e.strategy))
        before = len(cells)
        cells = [c for c in cells
                  if (c[0], c[1], edge_catalog._variant_name_from_config(c[2], c[3]))
                  in safe_keys]
        print(f"Filtered to deploy_safe: {len(cells)} of {before}")

    if args.limit > 0:
        cells = cells[:args.limit]
        print(f"Limited to first {len(cells)}")

    if args.dry_run:
        for sym, tf, strat, cfg in cells[:30]:
            print(f"  WOULD re-run: {sym:<14s}  {tf:<4s}  {strat:<22s}  {cfg[:60]}")
        if len(cells) > 30:
            print(f"  ... and {len(cells)-30} more")
        print(f"\nDry-run only — no writes. Remove --dry-run to execute.")
        return 0

    strategies = discover_strategies()
    print(f"Strategies discovered: {len(strategies)}\n")

    t0 = time.time()
    counts = {"ok": 0, "skipped": 0, "error": 0}
    for i, (sym, tf, strat, cfg) in enumerate(cells, 1):
        print(f"[{i:>3d}/{len(cells)}]  {sym:<14s} {tf:<4s} {strat:<22s} ", end="",
              flush=True)
        try:
            res = _run_one(sym, tf, strat, cfg,
                            db_path=db_path, strategies=strategies)
        except Exception as e:
            res = {"status": "error", "reason": f"unexpected: {e}"}
            traceback.print_exc()
        counts[res["status"]] = counts.get(res["status"], 0) + 1
        if res["status"] == "ok":
            print(f"  ✅ n={res['n']:>4d}  pnl=${res['pnl']:+9.2f}")
        elif res["status"] == "skipped":
            print(f"  ⏭️  {res['reason']}")
        else:
            print(f"  ❌ {res['reason']}")

    elapsed = time.time() - t0
    print(f"\n{'=' * 70}")
    print(f"  DONE in {elapsed:.1f}s — "
          f"{counts.get('ok', 0)} re-baselined · "
          f"{counts.get('skipped', 0)} skipped · "
          f"{counts.get('error', 0)} errored")
    print(f"  All re-baselined cells were run with: "
          f"{cost_defaults.cost_config_badge()}")
    print(f"  Reload Composer / Library / Backtest — same numbers everywhere.")
    print(f"{'=' * 70}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
