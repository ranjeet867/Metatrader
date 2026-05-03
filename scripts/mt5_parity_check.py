#!/usr/bin/env python3
"""
mt5_parity_check.py — compare Python backtest trades vs MT5 Strategy Tester
report (CSV).

Pipeline (Phase 26):
  1. python scripts/strategy_to_mql5.py --strategy <name>
  2. Compile in MetaEditor, drop on the matching chart, run Strategy Tester.
  3. Right-click report → Save → CSV → put under
        data/mt5_tester_reports/<strategy>_<symbol>_<tf>.csv
  4. python scripts/mt5_parity_check.py \\
        --strategy <name> --ticker <s> --tf <tf>

Compares:
  - Trade count: must be within ±5%
  - Total P&L: must be within ±10%
  - Per-trade matching by entry-time (tolerance ±1 bar)

Output: docs/parity_<strategy>_<ticker>_<tf>.md
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.backtest import partition_train_test, run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402


def _python_trades(strategy_name: str, symbol: str, tf: str) -> pd.DataFrame:
    """Run the Python backtest with realistic friction; return trades DF."""
    from importlib import import_module
    mod = import_module(f"strategies.{strategy_name}")
    StratCls = next(
        c for n, c in vars(mod).items()
        if isinstance(c, type) and getattr(c, "name", None) == strategy_name
    )
    ParamsCls_candidates = [c for n, c in vars(mod).items()
                              if isinstance(c, type) and n.endswith("Params")]
    ParamsCls = ParamsCls_candidates[0] if ParamsCls_candidates else None
    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())

    path = REPO / "data" / f"{symbol}_{tf}.parquet"
    df = load_parquet(path)
    r = run_backtest(
        df, strat.signals(df),
        starting_balance=100_000, lots=1.0, money_per_unit_price=1.0,
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        symbol=symbol,
        enforce_weekend_flat=True, enforce_daily_flat=True,
    )
    rows = []
    for t in r.trades:
        rows.append({
            "entry_time": df["time"].iloc[t.entry_bar_idx],
            "exit_time":  df["time"].iloc[t.exit_bar_idx],
            "direction":  t.direction,
            "entry":      t.entry_price,
            "exit":       t.exit_price,
            "pnl":        t.realized_pnl,
        })
    return pd.DataFrame(rows)


def _mt5_trades(csv_path: Path) -> pd.DataFrame:
    """Parse MT5 Strategy Tester CSV. Tries common column shapes; if the
    file isn't recognised, raise with the columns observed."""
    df = pd.read_csv(csv_path)
    # Common MT5 export shape includes columns like:
    #   Time, Type, Order, Size, Price, S/L, T/P, Profit
    # We map to entry_time / exit_time / direction / entry / exit / pnl
    cols_l = {c.lower(): c for c in df.columns}
    aliases = {
        "entry_time": ["open time", "open_time", "time", "datetime"],
        "exit_time":  ["close time", "close_time"],
        "direction":  ["type", "direction"],
        "entry":      ["open price", "open_price", "price open", "open"],
        "exit":       ["close price", "close_price", "price close", "close"],
        "pnl":        ["profit", "p/l", "pnl", "p&l"],
    }
    out = {}
    for target, opts in aliases.items():
        for o in opts:
            if o in cols_l:
                out[target] = df[cols_l[o]]
                break
    if "entry_time" not in out or "pnl" not in out:
        raise ValueError(
            f"could not parse MT5 report (got columns: {list(df.columns)})"
        )
    res = pd.DataFrame(out)
    res["entry_time"] = pd.to_datetime(res["entry_time"], utc=True,
                                          errors="coerce")
    if "exit_time" in res.columns:
        res["exit_time"] = pd.to_datetime(res["exit_time"], utc=True,
                                              errors="coerce")
    res["pnl"] = pd.to_numeric(res["pnl"], errors="coerce")
    res = res.dropna(subset=["entry_time", "pnl"])
    return res.reset_index(drop=True)


def compare(py: pd.DataFrame, mt5: pd.DataFrame, *, time_tolerance_bars: int = 1,
            tf_seconds: int = 86400) -> dict:
    """Return a dict with comparison metrics."""
    n_py, n_mt5 = len(py), len(mt5)
    pnl_py, pnl_mt5 = float(py["pnl"].sum()), float(mt5["pnl"].sum())
    count_diff_pct = abs(n_py - n_mt5) / max(1, max(n_py, n_mt5)) * 100
    pnl_diff_pct = (abs(pnl_py - pnl_mt5) / max(1, max(abs(pnl_py), abs(pnl_mt5)))
                     * 100 if max(abs(pnl_py), abs(pnl_mt5)) > 0 else 0)

    # Per-trade match: for each Python trade, find an MT5 trade within
    # ±tolerance bars on entry_time. Greedy match.
    matched_py = set()
    matched_mt5 = set()
    tol = pd.Timedelta(seconds=tf_seconds * time_tolerance_bars)
    for i, py_row in py.iterrows():
        candidates = mt5[
            (mt5["entry_time"] >= py_row["entry_time"] - tol)
            & (mt5["entry_time"] <= py_row["entry_time"] + tol)
        ]
        for j, _row in candidates.iterrows():
            if j in matched_mt5:
                continue
            matched_py.add(i); matched_mt5.add(j)
            break
    match_pct = (len(matched_py) / n_py * 100) if n_py else 0
    return {
        "n_py": n_py, "n_mt5": n_mt5,
        "pnl_py": pnl_py, "pnl_mt5": pnl_mt5,
        "count_diff_pct": count_diff_pct,
        "pnl_diff_pct": pnl_diff_pct,
        "match_pct": match_pct,
        "py_only": n_py - len(matched_py),
        "mt5_only": n_mt5 - len(matched_mt5),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--tf", required=True)
    ap.add_argument("--report",
                     help="path to MT5 CSV (default: data/mt5_tester_reports/"
                          "<strategy>_<ticker>_<tf>.csv)")
    args = ap.parse_args()

    csv_path = Path(args.report) if args.report else (
        REPO / "data" / "mt5_tester_reports"
        / f"{args.strategy}_{args.ticker}_{args.tf}.csv"
    )
    if not csv_path.exists():
        raise SystemExit(f"MT5 report not found: {csv_path}")

    py = _python_trades(args.strategy, args.ticker, args.tf)
    mt5 = _mt5_trades(csv_path)
    tf_sec = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}.get(args.tf, 86400)
    cmp_ = compare(py, mt5, tf_seconds=tf_sec)

    pass_count = cmp_["count_diff_pct"] <= 5.0
    pass_pnl = cmp_["pnl_diff_pct"] <= 10.0
    pass_match = cmp_["match_pct"] >= 80.0
    overall = pass_count and pass_pnl and pass_match

    out = REPO / "docs" / f"parity_{args.strategy}_{args.ticker}_{args.tf}.md"
    out.parent.mkdir(exist_ok=True)
    body = f"""# MT5 parity check — `{args.strategy}` `{args.ticker}` `{args.tf}`

Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}

## Verdict: **{'PASS ✅' if overall else 'FAIL ⛔'}**

| metric | Python | MT5 | Δ% | tolerance | result |
|---|---:|---:|---:|---|:---:|
| trade count | {cmp_['n_py']} | {cmp_['n_mt5']} | {cmp_['count_diff_pct']:.1f}% | ±5% | {'✅' if pass_count else '⛔'} |
| total P&L | ${cmp_['pnl_py']:+,.2f} | ${cmp_['pnl_mt5']:+,.2f} | {cmp_['pnl_diff_pct']:.1f}% | ±10% | {'✅' if pass_pnl else '⛔'} |
| per-trade match | — | — | {cmp_['match_pct']:.1f}% | ≥80% | {'✅' if pass_match else '⛔'} |

- Python-only trades (not seen in MT5): **{cmp_['py_only']}**
- MT5-only trades (not seen in Python): **{cmp_['mt5_only']}**

If counts diverge: likely intra-bar entries MT5 sees but Python misses
(Python signals fire only on bar-close).

If PnL diverges but counts match: spread/swap/commission differences —
adjust commission_per_trade to match the broker's fee schedule.

If per-trade match is low: investigate timing differences. The tolerance
is ±1 bar; tighter would be too strict for Strategy Tester's tick-mode.
"""
    out.write_text(body)
    print(f"[mt5-parity] verdict: {'PASS' if overall else 'FAIL'}")
    print(f"[mt5-parity] wrote {out.relative_to(REPO)}")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
