#!/usr/bin/env python3
"""
sweep_indices.py — index-focused sweep with the 6 new index strategies.

Tests every (strategy, ticker, tf) cell from STRATEGY_TF_MAP across:
  Indices:   US100.cash, US500.cash, GER40.cash, EU50.cash
  FX (sanity): EURUSD, USDJPY  — should mostly fail on index-specific strategies.

Friction in every cell:
  $3 commission per round-trip, 0.1×ATR slippage per fill, 60/40 train/test.

Acceptance criteria for "edge candidate":
  - reconciliation passes (mandatory)
  - n_test >= 25
  - test_PF >= 1.20
  - test_avg_R >= 0.10
  - train AND test BOTH positive (avg_R > 0, PF >= 1.0)

Halts (exit 3) on ANY reconciliation failure.

Outputs:
  - docs/index_edge_findings.md   (full markdown report)
  - docs/equity_<strategy>_<ticker>_<tf>.png   (one per surviving cell)
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from core.backtest import partition_train_test, run_backtest
from core.data import load_parquet
from strategies.first30_meanrev import First30MeanRev, First30MeanRevParams
from strategies.ibs import Ibs, IbsParams
from strategies.inside_bar import InsideBar, InsideBarParams
from strategies.orb import Orb, OrbParams
from strategies.overnight_drift import OvernightDrift, OvernightDriftParams
from strategies.vol_breakout import VolBreakout, VolBreakoutParams


# Per-ticker (money_per_unit_USD_per_lot, default_lots).
# Sized so that 1×ATR per trade ≈ a few-hundred USD on a $91k account.
TICKER_CONFIG: dict[str, tuple[float, float]] = {
    "US100.cash": (1.0,        6.5),
    "US500.cash": (1.0,       20.0),
    "GER40.cash": (1.0,        3.0),
    "EU50.cash":  (1.10,      20.0),
    "EURUSD":     (100_000.0,  1.0),
    "USDJPY":     (700.0,      1.0),
}

# Each strategy maps to the timeframes it's MEANT to be tested on.
STRATEGY_TF_MAP: dict[str, list[str]] = {
    "ibs":              ["M15", "H1", "D1"],
    "overnight_drift":  ["D1"],
    "orb":              ["M15"],
    "inside_bar":       ["H1", "D1"],
    "vol_breakout":     ["H1", "D1"],
    "first30_meanrev":  ["M15"],
}


def build_strategy(name: str, long_only: bool):
    if name == "ibs":
        return Ibs(IbsParams(long_only=long_only))
    if name == "overnight_drift":
        return OvernightDrift(OvernightDriftParams(long_only=True))  # always long
    if name == "orb":
        return Orb(OrbParams(long_only=long_only))
    if name == "inside_bar":
        return InsideBar(InsideBarParams(long_only=long_only))
    if name == "vol_breakout":
        return VolBreakout(VolBreakoutParams(long_only=long_only))
    if name == "first30_meanrev":
        return First30MeanRev(First30MeanRevParams(long_only=long_only))
    raise ValueError(f"unknown strategy: {name}")


@dataclass
class Cell:
    ticker: str
    tf: str
    strategy: str
    long_only: bool
    n_total: int
    n_train: int
    n_test: int
    train_pf: float
    test_pf: float
    train_R: float
    test_R: float
    train_ret_pct: float
    test_ret_pct: float
    bal_start: float
    bal_end: float
    reconciles: bool
    backtest_result: object = None    # the BacktestResult (for equity-curve plotting)


def fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def is_edge_candidate(c: Cell, min_n_test: int, min_pf: float, min_R: float) -> bool:
    return (
        c.reconciles
        and c.n_test >= min_n_test
        and c.test_pf >= min_pf
        and c.test_R >= min_R
        and c.train_R > 0
        and c.train_pf >= 1.0
    )


def plot_equity_curve(cell: Cell, df_full, balance_start: float,
                        out_path: Path) -> None:
    """Plot the full-period equity curve with train/test boundary marked."""
    r = cell.backtest_result
    if r is None:
        return
    eq = r.equity_curve
    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(eq["time"], eq["equity"], linewidth=1.0, color="#0a4")
    ax.axhline(balance_start, color="#888", linestyle="--", linewidth=0.7,
                label=f"start ${balance_start:,.0f}")
    # Train/test boundary
    split_idx = int(len(df_full) * 0.6)
    if 0 < split_idx < len(df_full):
        split_time = df_full["time"].iloc[split_idx]
        ax.axvline(split_time, color="#c33", linestyle=":", linewidth=0.9,
                    label="train / test split")
    title = (f"{cell.strategy} on {cell.ticker} {cell.tf}"
             f"  ({'long' if cell.long_only else 'long+short'})\n"
             f"train PF={fmt_pf(cell.train_pf)} R={cell.train_R:+.3f}  |  "
             f"test PF={fmt_pf(cell.test_pf)} R={cell.test_R:+.3f}  "
             f"n_test={cell.n_test}")
    ax.set_title(title, fontsize=10)
    ax.set_xlabel("time")
    ax.set_ylabel("equity ($)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=91_400)
    ap.add_argument("--commission-per-trade", type=float, default=3.0)
    ap.add_argument("--slippage-atr-frac", type=float, default=0.1)
    ap.add_argument("--train-pct", type=float, default=0.6)
    ap.add_argument("--min-test-trades", type=int, default=25)
    ap.add_argument("--min-test-pf", type=float, default=1.20)
    ap.add_argument("--min-test-R", type=float, default=0.10)
    ap.add_argument("--out", default=str(ROOT / "docs" / "index_edge_findings.md"))
    args = ap.parse_args()

    print(f"\n{'=' * 80}")
    print(f"  INDEX-FOCUSED SWEEP — 6 new strategies + their natural TFs")
    print(f"  friction: comm=${args.commission_per_trade}  "
          f"slip={args.slippage_atr_frac}×ATR  "
          f"train_pct={args.train_pct}")
    print(f"  edge: n_test>={args.min_test_trades} test_PF>={args.min_test_pf} "
          f"test_R>={args.min_test_R} train+test both positive")
    print(f"{'=' * 80}\n")

    cells: list[Cell] = []
    skipped = []
    failed_reconcile = []

    # We try BOTH long_only and bidirectional for each cell, except overnight_drift
    # which is long-only by definition.
    modes_per_strategy = {
        "ibs":              [True],            # IBS as written is long-only
        "overnight_drift":  [True],
        "orb":              [True, False],
        "inside_bar":       [True, False],
        "vol_breakout":     [True, False],
        "first30_meanrev":  [True, False],
    }

    for ticker, (mpu, default_lots) in TICKER_CONFIG.items():
        for strat_name, tfs in STRATEGY_TF_MAP.items():
            for tf in tfs:
                parquet = ROOT / "data" / f"{ticker}_{tf}.parquet"
                if not parquet.exists():
                    skipped.append(f"{ticker}_{tf}")
                    continue
                df = load_parquet(parquet)
                if len(df) < 200:
                    skipped.append(f"{ticker}_{tf} ({len(df)} bars)")
                    continue
                for long_only in modes_per_strategy[strat_name]:
                    strat = build_strategy(strat_name, long_only)
                    sigs = strat.signals(df)
                    r = run_backtest(
                        df, sigs,
                        starting_balance=args.balance,
                        lots=default_lots,
                        money_per_unit_price=mpu,
                        commission_per_trade=args.commission_per_trade,
                        slippage_per_fill_atr_frac=args.slippage_atr_frac,
                    )
                    if not r.reconciles:
                        failed_reconcile.append(
                            f"{ticker}/{tf}/{strat_name}/{'long' if long_only else 'bidir'}"
                        )
                        continue
                    train, test = partition_train_test(r, args.train_pct, n_bars=len(df))
                    cell = Cell(
                        ticker=ticker, tf=tf, strategy=strat_name,
                        long_only=long_only,
                        n_total=r.n_trades,
                        n_train=train.n_trades, n_test=test.n_trades,
                        train_pf=train.profit_factor, test_pf=test.profit_factor,
                        train_R=train.avg_R, test_R=test.avg_R,
                        train_ret_pct=train.sum_pnl / args.balance * 100.0,
                        test_ret_pct=test.sum_pnl / args.balance * 100.0,
                        bal_start=args.balance, bal_end=r.ending_balance,
                        reconciles=r.reconciles,
                        backtest_result=r,
                    )
                    cells.append(cell)

    if failed_reconcile:
        print(f"⛔ {len(failed_reconcile)} cells FAILED reconciliation:")
        for f in failed_reconcile:
            print(f"   {f}")
        return 3

    if skipped:
        print(f"  (skipped: {', '.join(skipped[:10])}{' …' if len(skipped) > 10 else ''})")

    survivors = [c for c in cells
                 if is_edge_candidate(c, args.min_test_trades,
                                        args.min_test_pf, args.min_test_R)]

    # Sort survivors by test_R desc
    survivors.sort(key=lambda c: (c.test_R, c.n_test), reverse=True)
    cells_sorted_by_test_R = sorted(cells, key=lambda c: c.test_R, reverse=True)

    print(f"\n  TOP 30 cells by test_R:\n")
    print(f"  {'ticker':12s} {'tf':>4s} {'strategy':18s} {'mode':>6s} "
          f"{'n_tr':>5s} {'tr_PF':>6s} {'tr_R':>7s} | "
          f"{'n_te':>5s} {'te_PF':>6s} {'te_R':>7s}  surv?")
    print("  " + "─" * 110)
    for c in cells_sorted_by_test_R[:30]:
        is_surv = c in survivors
        marker = "⭐" if is_surv else "  "
        mode = "long" if c.long_only else "bidir"
        print(f"  {c.ticker:12s} {c.tf:>4s} {c.strategy:18s} {mode:>6s} "
              f"{c.n_train:>5d} {fmt_pf(c.train_pf):>6s} {c.train_R:>+7.3f} | "
              f"{c.n_test:>5d} {fmt_pf(c.test_pf):>6s} {c.test_R:>+7.3f}  {marker}")

    print(f"\n  Total cells run:    {len(cells)}")
    print(f"  Survivors meeting acceptance criteria: {len(survivors)}")

    if survivors:
        print("\n  ⭐ EDGE CANDIDATES:\n")
        for c in survivors:
            mode = "long" if c.long_only else "bidir"
            print(f"    {c.ticker:12s} {c.tf:>4s} {c.strategy:18s} {mode:>6s}  "
                  f"test_R={c.test_R:+.3f}  test_PF={fmt_pf(c.test_pf)}  "
                  f"n_test={c.n_test}  test_ret={c.test_ret_pct:+.2f}%")

    # ---- equity curves for survivors ----
    docs = ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for c in survivors:
        df = load_parquet(ROOT / "data" / f"{c.ticker}_{c.tf}.parquet")
        mode = "long" if c.long_only else "bidir"
        png = docs / f"equity_{c.strategy}_{c.ticker}_{c.tf}_{mode}.png"
        plot_equity_curve(c, df, args.balance, png)
        print(f"    plotted {png.name}")

    # ---- markdown ----
    lines = []
    lines.append("# Index Edge Findings (6 new strategies)")
    lines.append("")
    lines.append(f"- Friction: ${args.commission_per_trade}/trade commission, "
                  f"{args.slippage_atr_frac}×ATR slippage per fill, "
                  f"{int(args.train_pct*100)}/{int((1-args.train_pct)*100)} train/test split")
    lines.append(f"- Acceptance: n_test ≥ {args.min_test_trades}, "
                  f"test_PF ≥ {args.min_test_pf}, test_R ≥ {args.min_test_R}, "
                  f"train_R > 0, train_PF ≥ 1.0")
    lines.append(f"- Total cells run: **{len(cells)}**")
    lines.append(f"- Survivors: **{len(survivors)}**")
    lines.append("")
    if survivors:
        lines.append("## ⭐ Edge candidates")
        lines.append("")
        lines.append("| ticker | tf | strategy | mode | n_train | train_PF | train_R | n_test | test_PF | test_R | test_ret % |")
        lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for c in survivors:
            mode = "long" if c.long_only else "bidir"
            png = f"equity_{c.strategy}_{c.ticker}_{c.tf}_{mode}.png"
            lines.append(
                f"| [{c.ticker}](./{png}) | {c.tf} | {c.strategy} | {mode} | "
                f"{c.n_train} | {fmt_pf(c.train_pf)} | {c.train_R:+.3f} | "
                f"{c.n_test} | {fmt_pf(c.test_pf)} | {c.test_R:+.3f} | "
                f"{c.test_ret_pct:+.2f}% |"
            )
        lines.append("")
    else:
        lines.append("## No edge candidates")
        lines.append("")
        lines.append(f"Across all 6 new strategies × 4 indices × 2 FX × allowed TFs ({len(cells)} cells), "
                      "**no cell met the acceptance criteria**. Honest result. The new strategies, like "
                      "the first batch, do not survive realistic friction + OOS partitioning at the lot-sizing "
                      "and timeframes tested. See full table below for closest misses.")
        lines.append("")
    lines.append("## Top 30 cells by test_R")
    lines.append("")
    lines.append("| ticker | tf | strategy | mode | n_train | train_PF | train_R | n_test | test_PF | test_R | survivor |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|:---:|")
    for c in cells_sorted_by_test_R[:30]:
        is_surv = c in survivors
        mode = "long" if c.long_only else "bidir"
        lines.append(
            f"| {c.ticker} | {c.tf} | {c.strategy} | {mode} | "
            f"{c.n_train} | {fmt_pf(c.train_pf)} | {c.train_R:+.3f} | "
            f"{c.n_test} | {fmt_pf(c.test_pf)} | {c.test_R:+.3f} | "
            f"{'⭐' if is_surv else ''} |"
        )
    lines.append("")
    lines.append("## Comparison vs yesterday's best FX cells")
    lines.append("")
    lines.append("Yesterday's best (from `docs/findings.md`):")
    lines.append("- EURUSD M15 rsi_30_70 long-only: n=45, test_PF=1.44, test_R=+0.233")
    lines.append("- EURUSD D1 ema_cross_9_20 bidir: n=22, test_PF=1.67, test_R=+0.435")
    lines.append("")
    lines.append("Best NEW cells (this run):")
    if survivors:
        for c in survivors[:3]:
            mode = "long" if c.long_only else "bidir"
            lines.append(f"- {c.ticker} {c.tf} {c.strategy} {mode}: "
                          f"n={c.n_test}, test_PF={fmt_pf(c.test_pf)}, "
                          f"test_R={c.test_R:+.3f}")
    else:
        lines.append("(none — all new strategies failed acceptance)")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    if survivors:
        s = survivors[0]
        mode = "long" if s.long_only else "bidir"
        lines.append(f"Top candidate: **{s.ticker} {s.tf} {s.strategy} ({mode})**")
        lines.append(f"- n_test = {s.n_test}, test_PF = {fmt_pf(s.test_pf)}, test_R = {s.test_R:+.3f}")
        lines.append(f"- Train period: {s.train_ret_pct:+.2f}% return")
        lines.append(f"- Test period: {s.test_ret_pct:+.2f}% return")
        lines.append("")
        lines.append("Suggested next step: port to MQL5, run in MT5 Strategy Tester, verify trade count and total P&L within ±10% before paper-trading.")
    else:
        lines.append("No new candidates. Stick with yesterday's FX survivors:")
        lines.append("1. **EURUSD M15 rsi_30_70 long-only** (most data, 45 OOS trades, PF 1.44)")
        lines.append("2. **EURUSD D1 ema_cross_9_20 bidirectional** (strongest PF, 22 OOS trades)")
        lines.append("")
        lines.append("Indices appear to NOT have exploitable retail edge in any of the 6 new strategies "
                      "tested at the lot-sizing and timeframes covered. Next directions to consider: "
                      "ML-based signals, cross-asset strategies, or accept the FX-only conclusion.")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
