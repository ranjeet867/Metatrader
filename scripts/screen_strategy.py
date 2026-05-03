#!/usr/bin/env python3
"""
screen_strategy.py — single command to vet a new strategy.

Pipeline:
  1. Verify the strategy class is importable and has the expected shape.
  2. Run reconciliation on each (ticker, tf) cell on the cached parquets.
  3. Sweep 8 tickers × 3 TFs × default params (or just one if --ticker / --tf).
  4. Identify the best (ticker, tf) cell by test_R.
  5. Compare best cell vs portfolio threshold (PF >= 1.3 AND R >= 0.15 AND
     n_test >= 25).
  6. Output a verdict: ADD / DISCARD / NEEDS_TUNING with explanation.
  7. Write docs/<name>_screening.md with the full report.

Usage:
    python scripts/screen_strategy.py --strategy ibs
    python scripts/screen_strategy.py --strategy ibs --ticker US100.cash --tf D1
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402

DEFAULT_MONEY_PER_UNIT = {
    "US100.cash": 1.0, "US500.cash": 1.0, "GER40.cash": 1.0,
    "EU50.cash":  1.10,
    "EURUSD": 100_000.0, "GBPUSD": 100_000.0, "AUDUSD": 100_000.0,
    "NZDUSD": 100_000.0, "USDJPY": 700.0, "GBPJPY": 700.0,
}
DEFAULT_LOTS = {
    "US100.cash": 6.5, "US500.cash": 20.0, "GER40.cash": 3.0,
    "EU50.cash": 20.0,
    "EURUSD": 1.0, "GBPUSD": 1.0, "AUDUSD": 1.5, "NZDUSD": 1.5,
    "USDJPY": 1.0, "GBPJPY": 0.7,
}


def _import_strategy(name: str):
    """Find strategies/<name>.py and return (StrategyClass, ParamsClass)."""
    mod_name = f"strategies.{name}"
    try:
        mod = importlib.import_module(mod_name)
    except Exception as e:
        raise SystemExit(f"could not import {mod_name}: {e}")
    strat_cls = None
    params_cls = None
    for clsname, cls in inspect.getmembers(mod, inspect.isclass):
        if cls.__module__ != mod.__name__:
            continue
        if dataclasses.is_dataclass(cls) and clsname.endswith("Params"):
            params_cls = cls
        elif (hasattr(cls, "name") and isinstance(cls.name, str)
              and hasattr(cls, "signals")
              and not dataclasses.is_dataclass(cls)):
            strat_cls = cls
    if strat_cls is None:
        raise SystemExit(f"no strategy class with name+signals found in {mod_name}")
    return strat_cls, params_cls


def _discover_data() -> dict:
    out = {}
    for path in sorted((REPO / "data").glob("*.parquet")):
        stem = path.stem
        if "_" not in stem:
            continue
        ticker, tf = stem.rsplit("_", 1)
        out.setdefault(ticker, {})[tf] = path
    return out


def screen_one(strat, ticker: str, tf: str, path: Path) -> dict:
    """Backtest one cell with realistic friction; return result dict."""
    try:
        df = load_parquet(path)
    except Exception as e:
        return {"ticker": ticker, "tf": tf, "error": f"load: {e}"}
    try:
        sigs = strat.signals(df)
    except Exception as e:
        return {"ticker": ticker, "tf": tf, "error": f"signals: {e}"}
    try:
        r = run_backtest(
            df, sigs,
            starting_balance=91_400,
            lots=DEFAULT_LOTS.get(ticker, 0.1),
            money_per_unit_price=DEFAULT_MONEY_PER_UNIT.get(ticker, 1.0),
            commission_per_trade=3.0,
            slippage_per_fill_atr_frac=0.1,
            symbol=ticker,
            enforce_weekend_flat=True,
            enforce_daily_flat=True,
        )
    except Exception as e:
        return {"ticker": ticker, "tf": tf, "error": f"backtest: {e}"}
    if not r.reconciles:
        return {"ticker": ticker, "tf": tf,
                "error": f"reconciliation failed div=${r.reconcile_divergence:+.2f}"}
    train, test = partition_train_test(r, 0.6, n_bars=len(df))
    return {
        "ticker": ticker, "tf": tf,
        "n_train": train.n_trades, "n_test": test.n_trades,
        "train_PF": train.profit_factor, "test_PF": test.profit_factor,
        "train_R": train.avg_R, "test_R": test.avg_R,
        "test_pnl": test.sum_pnl,
        "reconciles": r.reconciles,
    }


def verdict_for(best: dict | None) -> tuple[str, str]:
    """Return (verdict, explanation)."""
    if best is None or "error" in best:
        return ("DISCARD",
                "No cell passed reconciliation — root-cause first.")
    pf = best.get("test_PF", 0)
    R = best.get("test_R", 0)
    n = best.get("n_test", 0)
    if n < 25:
        return ("NEEDS_TUNING",
                f"best cell only {n} OOS trades — too few to assess "
                "(target n_test >= 25). Tune params or expand history.")
    pf_ok = pf >= 1.3 and pf != float("inf")
    R_ok = R >= 0.15
    if pf_ok and R_ok:
        return ("ADD",
                f"best cell PF={pf:.2f}, R={R:+.3f}, n={n} — meets the "
                "PF>=1.3 / R>=0.15 / n>=25 threshold.")
    if not pf_ok and R_ok:
        return ("NEEDS_TUNING",
                f"R looks good (R={R:+.3f}) but PF={pf:.2f} below 1.3. "
                "Consider tightening stops or filtering by regime.")
    if pf_ok and not R_ok:
        return ("NEEDS_TUNING",
                f"PF={pf:.2f} OK but per-trade R={R:+.3f} small. "
                "Larger targets or longer holds may help.")
    return ("DISCARD",
            f"best cell PF={pf:.2f} / R={R:+.3f} — no edge after friction.")


def render_report(name: str, rows: list[dict], best: dict | None,
                   verdict: str, explanation: str) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# Strategy screening — `{name}`",
        f"",
        f"Generated {now}",
        f"",
        f"## Verdict: **{verdict}**",
        f"",
        f"{explanation}",
        f"",
        f"## All cells (with realistic friction: $3 commission, 0.1×ATR slippage,",
        f"60/40 train/test, time guards ON)",
        f"",
        "| ticker | tf | n_train | n_test | train_PF | test_PF | train_R | test_R | test_$ | note |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['ticker']} | {r['tf']} | — | — | — | — | — | — | — | "
                          f"{r['error']} |")
            continue
        pf_train = (f"{r['train_PF']:.2f}" if r['train_PF'] != float('inf')
                    else "inf")
        pf_test = (f"{r['test_PF']:.2f}" if r['test_PF'] != float('inf')
                    else "inf")
        lines.append(
            f"| {r['ticker']} | {r['tf']} | {r['n_train']} | {r['n_test']} | "
            f"{pf_train} | {pf_test} | {r['train_R']:+.3f} | {r['test_R']:+.3f} | "
            f"${r['test_pnl']:+,.0f} | |"
        )
    if best:
        lines.append("")
        lines.append(f"**Best cell:** `{best['ticker']}` `{best['tf']}` — "
                      f"PF={best.get('test_PF', 0):.2f}, "
                      f"R={best.get('test_R', 0):+.3f}, "
                      f"n_test={best.get('n_test', 0)}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True,
                     help="strategy module name under strategies/ (e.g. 'ibs')")
    ap.add_argument("--ticker", default=None,
                     help="restrict to this ticker only")
    ap.add_argument("--tf", default=None,
                     help="restrict to this tf only")
    args = ap.parse_args()

    StratCls, ParamsCls = _import_strategy(args.strategy)
    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    print(f"[screen] strategy={strat.name}")

    data = _discover_data()
    if not data:
        raise SystemExit("no parquets in data/ — run make refresh-data first")

    cells = []
    for ticker, tfs in sorted(data.items()):
        if args.ticker and ticker != args.ticker:
            continue
        for tf, path in sorted(tfs.items()):
            if args.tf and tf != args.tf:
                continue
            cells.append((ticker, tf, path))
    if not cells:
        raise SystemExit("no cells matched the filters")

    rows = []
    for i, (ticker, tf, path) in enumerate(cells, start=1):
        print(f"  [{i}/{len(cells)}] {ticker} {tf}", end=" ", flush=True)
        # Build a fresh strat each iteration in case it accumulates state
        strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
        r = screen_one(strat, ticker, tf, path)
        rows.append(r)
        if "error" in r:
            print(f"ERROR: {r['error']}")
        else:
            print(f"PF={r['test_PF']:.2f} R={r['test_R']:+.3f} n={r['n_test']}")

    successes = [r for r in rows if "error" not in r]
    best = (max(successes, key=lambda x: x["test_R"]) if successes else None)
    verdict, explanation = verdict_for(best)
    print(f"\n[screen] verdict: {verdict}")
    print(f"[screen] {explanation}")

    out_path = REPO / "docs" / f"{args.strategy}_screening.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(render_report(args.strategy, rows, best,
                                        verdict, explanation))
    print(f"[screen] wrote {out_path.relative_to(REPO)}")
    sys.exit(0 if verdict == "ADD" else 1)


if __name__ == "__main__":
    main()
