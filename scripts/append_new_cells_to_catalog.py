#!/usr/bin/env python3
"""
append_new_cells_to_catalog.py — generate a fresh
docs/optimization_*.md that includes both the existing top survivors
AND the new candidates we backtested today (XAUUSD, CSCO, AMZN, etc.).

Why this exists:
  Composer / Strategy Compare / Strategy Library all read their
  candidate list from `docs/optimization_*.md` (latest by name). They
  do NOT auto-discover from data/v2.db. So when run_backtest.py
  persists a new run, the dashboards still show the old list.

This script re-runs the canonical NEW cells with cost-priced backtests
(realistic commission + slippage + per-instrument tick value) and
appends them to the existing catalog ordered by score.

Usage:
    python scripts/append_new_cells_to_catalog.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.compare_top_cells import CELLS    # noqa: E402
from scripts.sweep_rr_winrate import _backtest_one    # noqa: E402


def _rr_label(rr: float) -> str:
    return {1.0: "1:1", 1.1: "1:1.1", 1.2: "1:1.2",
              1.5: "1:1.5", 2.0: "1:2"}.get(round(rr, 2), f"1:{rr:.1f}")


def _row_md(rank: int, r) -> str:
    """One pipe-separated row matching the optimization_*.md schema."""
    side = "long" if r.long_only else "bidir"
    sus = "✅" if r.max_dd_pct < 10 else "❌"
    recov = (f"{int(r.recovery_days)}" if r.recovery_days is not None else "—")
    cagr = ""    # not computed in sweep — leave blank
    p_pass = ""  # not computed
    win_dollars_avg = (r.net_pnl_dollars / max(1, r.n_trades)
                          if r.win_rate_pct > 0 else 0)
    return (
        f"| {rank} | `{r.strategy}` | `{r.ticker}` | `{r.tf}` | "
        f"{side} | {_rr_label(r.rr)} | {r.n_trades} | "
        f"{r.profit_factor:.2f} | {r.avg_r:+.2f} | "
        f"{r.win_rate_pct:.0f} | {r.net_pnl_dollars:+,.0f} | "
        f"{r.max_dd_pct:.1f} | {r.max_dd_dollars:,.0f} | "
        f"— | {recov} | {r.max_consec_losses} | "
        f"{r.rr:.2f} | +{abs(win_dollars_avg):.0f} | "
        f"-{abs(win_dollars_avg)/2:.0f} | {cagr} | {p_pass} | "
        f"{sus} | {r.survival_score:.1f} |"
    )


def main() -> int:
    print(f"Re-running {len(CELLS)} cells with cost-priced backtests...")
    rows = []
    for i, (label, strat, ticker, tf, sm, tm, lo, group) in enumerate(CELLS):
        print(f"  [{i+1:2d}/{len(CELLS)}] {label} ", end="", flush=True)
        try:
            r = _backtest_one(
                strategy_name=strat, ticker=ticker, tf=tf,
                stop_mult=sm, target_mult=tm, long_only=lo,
                balance=100_000, lots=1.0, money_per_unit=1.0,
                train_pct=0.6, slip_atr_frac=0.05,
                commission_per_trade=4.0,
            )
            print("✓" if r else "(no row)")
            if r is not None:
                rows.append(r)
        except Exception as e:
            print(f"✗ {type(e).__name__}: {e}")

    # Sort by survival score
    rows.sort(key=lambda r: -r.survival_score)

    # Build markdown
    today = datetime.now(timezone.utc).date().isoformat()
    out = ROOT / "docs" / f"optimization_{today}_full.md"
    lines = [
        "# Portfolio optimization — full sweep (cost-priced)",
        "",
        f"- Run UTC: `{datetime.now(timezone.utc).isoformat()}`",
        f"- Cells: **{len(rows)}** cost-priced (commission $4 + slip 0.05×ATR)",
        f"- Per-instrument tick value auto-loaded from `data/symbol_info.json`",
        "- Lots: 1.0 / Money-per-unit: 1.0 / Starting balance: $100,000",
        "",
        "Score = +Kelly × √trades − DD/recovery/streak penalties.",
        "Cells with score > 0 PASS the survival gate.",
        "",
        "| rank | strategy | ticker | tf | side | R:R | n | PF | R | win% | "
        "netPnL$ | maxDD% | maxDD$ | DDdays | recovD | streak | rr | "
        "avgWin$ | avgLoss$ | CAGR | P(pass) | sus | score |",
        "|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
        "---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append(_row_md(i, r))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅  Wrote {len(rows)} cells → {out.relative_to(ROOT)}")
    print(f"   Composer / Strategy Compare / Strategy Library will read this "
          f"on next page load.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
