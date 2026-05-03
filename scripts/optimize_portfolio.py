#!/usr/bin/env python3
"""
optimize_portfolio.py — autonomous portfolio optimizer.

Runs every (strategy × ticker × tf × R:R variant) cell on the cached
parquets. For each cell it computes:

  - reconciliation gate (drops anything that fails)
  - FullStats including drawdown %, recovery days, R:R, max consec losses
  - FTMO Monte-Carlo P(pass) at the user's chosen risk %
  - sustained = equity never touched FTMO -10% floor

Then it scores each cell with `core.optimizer.score_cell` and writes
the top-N to `docs/optimization_<YYYY-MM-DD>.md`.

The dashboard's strategy_library reads that markdown so the
recommendations stay in sync with whatever the optimizer last produced.

Usage:
    python scripts/optimize_portfolio.py
    python scripts/optimize_portfolio.py --risk 0.5 --top 30
    python scripts/optimize_portfolio.py --tickers USDJPY,GBPJPY --tfs D1
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.backtest import run_backtest                                  # noqa: E402
from core.backtest_stats import compute_full_stats                       # noqa: E402
from core.data import load_parquet                                       # noqa: E402
from core.ftmo_simulator import StrategyDist, simulate_pass_rate          # noqa: E402
from core.optimizer import (                                              # noqa: E402
    RR_VARIANTS, CellScore, is_sustained,
    render_markdown, score_cell,
)

# Strategies to sweep — each entry is (display_name, factory(rr_cfg) → strategy).
# Only strategies that accept stop/target atr-mult overrides are swept across
# RR_VARIANTS. The rest are run with their defaults.
from strategies.bbands_meanrev import BBandsMeanRev, BBandsMeanRevParams
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.ema_cross import EmaCross, EmaCrossParams
from strategies.ema_pullback import EmaPullback, EmaPullbackParams
from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams


def _build_rr_aware(name: str):
    """Return a list of (label, factory) tuples — one per RR variant if
    the strategy accepts stop_atr_mult/target_atr_mult, else just one."""
    if name == "ema_cross_9_20":
        return [
            (rr.label, lambda lo, _r=rr: EmaCross(EmaCrossParams(
                fast_period=9, slow_period=20, long_only=lo,
                stop_atr_mult=_r.stop_atr_mult,
                target_atr_mult=_r.target_atr_mult)))
            for rr in RR_VARIANTS
        ]
    if name == "ema_cross_12_26":
        return [
            (rr.label, lambda lo, _r=rr: EmaCross(EmaCrossParams(
                fast_period=12, slow_period=26, long_only=lo,
                stop_atr_mult=_r.stop_atr_mult,
                target_atr_mult=_r.target_atr_mult)))
            for rr in RR_VARIANTS
        ]
    if name == "donchian_20":
        return [
            (rr.label, lambda lo, _r=rr: DonchianBreakout(
                DonchianBreakoutParams(period=20, long_only=lo,
                                         stop_atr_mult=_r.stop_atr_mult,
                                         target_atr_mult=_r.target_atr_mult)))
            for rr in RR_VARIANTS
        ]
    if name == "donchian_55":
        return [
            (rr.label, lambda lo, _r=rr: DonchianBreakout(
                DonchianBreakoutParams(period=55, long_only=lo,
                                         stop_atr_mult=_r.stop_atr_mult,
                                         target_atr_mult=_r.target_atr_mult)))
            for rr in RR_VARIANTS
        ]
    if name == "ema_pullback_20_50":
        return [(
            "default",
            lambda lo: EmaPullback(EmaPullbackParams(
                fast_period=20, slow_period=50, long_only=lo)))]
    if name == "rsi_30_70":
        return [(
            "default",
            lambda lo: RsiMeanRev(RsiMeanRevParams(
                oversold=30, overbought=70, long_only=lo)))]
    if name == "bbands_20_2":
        return [(
            "default",
            lambda lo: BBandsMeanRev(BBandsMeanRevParams(
                period=20, num_std=2.0, long_only=lo)))]
    raise ValueError(f"unknown strategy {name}")


STRATEGY_NAMES = [
    "ema_cross_9_20", "ema_cross_12_26",
    "donchian_20", "donchian_55",
    "ema_pullback_20_50", "rsi_30_70", "bbands_20_2",
]


def _ftmo_pass_rate(trades, risk_pct: float, *, n_iter: int = 2000,
                    bars_per_day: int = 1) -> float | None:
    """Bootstrap a P(pass) for this single strategy's OOS R distribution."""
    rs = np.array([t.r_multiple for t in trades], dtype=float)
    if rs.size == 0:
        return None
    n_oos_days = max(1.0, len(trades) / max(0.05, bars_per_day))
    tp_day = max(0.05, len(trades) / n_oos_days) if n_oos_days else 0.05
    pool = [StrategyDist(
        name="x", symbol="x", r_multiples=rs,
        trades_per_day=tp_day,
        risk_per_trade_pct=risk_pct,
    )]
    res = simulate_pass_rate(
        pool, starting_balance=100_000, days=30,
        daily_loss_cap_pct=5.0, total_loss_cap_pct=10.0,
        pass_target_pct=10.0, n_iterations=n_iter, seed=42,
    )
    return float(res.p_pass)


def _bars_per_day(tf: str) -> int:
    return {"M15": 96, "H1": 24, "H4": 6, "D1": 1}.get(tf, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--risk", type=float, default=0.5,
                     help="Per-trade risk percent for FTMO sim (default 0.5)")
    ap.add_argument("--top", type=int, default=30,
                     help="Top-N cells to surface in the report")
    ap.add_argument("--tickers", default=None,
                     help="Comma-separated tickers (default: all parquets)")
    ap.add_argument("--tfs", default="M15,H1,D1",
                     help="Comma-separated timeframes")
    ap.add_argument("--strategies", default=",".join(STRATEGY_NAMES),
                     help="Comma-separated strategy names")
    ap.add_argument("--ftmo-iter", type=int, default=2000,
                     help="Monte-Carlo iterations per cell")
    ap.add_argument("--require-sustained", action="store_true",
                     help="Drop cells that touched the FTMO -10% floor.")
    ap.add_argument("--out", default=None,
                     help="Output markdown path")
    args = ap.parse_args()

    tfs = [s.strip() for s in args.tfs.split(",") if s.strip()]
    strategies_to_run = [s.strip() for s in args.strategies.split(",")
                          if s.strip()]

    # Discover parquets
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = sorted({p.name.split("_")[0]
                            for p in (ROOT / "data").glob("*.parquet")
                            if "_" in p.name})

    print(f"Sweeping {len(strategies_to_run)} strategies × "
            f"{len(tickers)} tickers × {len(tfs)} TFs … "
            f"risk={args.risk}%, ftmo_iter={args.ftmo_iter}")

    rows: list[CellScore] = []
    n_run = 0
    n_skipped = 0
    for strategy_name in strategies_to_run:
        try:
            variants = _build_rr_aware(strategy_name)
        except ValueError:
            print(f"  ⚠ skip {strategy_name}: unsupported")
            continue
        for ticker in tickers:
            for tf in tfs:
                ppath = ROOT / "data" / f"{ticker}_{tf}.parquet"
                if not ppath.exists():
                    continue
                try:
                    df = load_parquet(ppath)
                except Exception:
                    continue
                bpd = _bars_per_day(tf)

                for rr_label, factory in variants:
                    try:
                        strat = factory(True)   # long-only
                        result = run_backtest(
                            df, strat.signals(df),
                            starting_balance=100_000,
                            lots=1.0, money_per_unit_price=1.0,
                            commission_per_trade=3.0,
                            slippage_per_fill_atr_frac=0.1,
                            symbol=ticker,
                        )
                    except Exception as e:
                        print(f"  skip {strategy_name} {ticker} {tf} {rr_label}: {e}")
                        n_skipped += 1
                        continue
                    if not result.reconciles:
                        print(f"  ⛔ recon fail {strategy_name} {ticker} {tf} {rr_label}")
                        n_skipped += 1
                        continue
                    n_run += 1
                    # Slice OOS portion (60/40)
                    split = int(len(df) * 0.6)
                    oos_trades = [t for t in result.trades
                                    if t.entry_bar_idx >= split]
                    oos_result = dataclasses.replace(result, trades=oos_trades)
                    stats = compute_full_stats(oos_result, starting_balance=100_000)
                    if stats.n_trades < 3:
                        # too few OOS trades — skip
                        continue
                    p_pass = _ftmo_pass_rate(oos_trades, args.risk,
                                                n_iter=args.ftmo_iter,
                                                bars_per_day=bpd)
                    sustained = is_sustained(result.equity_curve,
                                                baseline=100_000)

                    cs = CellScore(
                        strategy=strategy_name, ticker=ticker, tf=tf,
                        rr_label=rr_label,
                        stop_atr_mult=getattr(strat.params, "stop_atr_mult", 0.0)
                        if hasattr(strat, "params") else 0.0,
                        target_atr_mult=getattr(strat.params, "target_atr_mult", 0.0)
                        if hasattr(strat, "params") else 0.0,
                        risk_pct=args.risk,
                        n_test=stats.n_trades,
                        test_pf=stats.profit_factor,
                        test_r=stats.avg_R,
                        win_rate=stats.win_rate_pct,
                        max_dd_pct=stats.max_dd_pct,
                        recovery_days=stats.recovery_duration_days,
                        max_consec_losses=stats.max_consec_losses,
                        rr_ratio=stats.risk_reward_ratio,
                        cagr_pct=stats.cagr_pct,
                        p_pass_30d=p_pass,
                        score=score_cell(stats, p_pass=p_pass,
                                          sustained=sustained),
                        sustained=sustained,
                    )
                    rows.append(cs)

    # Rank
    if args.require_sustained:
        rows = [r for r in rows if r.sustained]
    rows.sort(key=lambda r: r.score, reverse=True)
    top = rows[: args.top]

    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = (Path(args.out) if args.out
                 else ROOT / "docs" / f"optimization_{when}.md")
    meta = (f"- Run UTC: `{datetime.now(timezone.utc).isoformat(timespec='seconds')}`\n"
             f"- Strategies: `{', '.join(strategies_to_run)}`\n"
             f"- Tickers: `{', '.join(tickers)}`\n"
             f"- Timeframes: `{', '.join(tfs)}`\n"
             f"- Risk %/trade: `{args.risk}`\n"
             f"- Cells run: **{n_run}**, skipped: **{n_skipped}**\n"
             f"- Sustained-only filter: `{args.require_sustained}`\n")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(top, run_meta=meta))
    print(f"\n✓ Wrote {out_path}  (top {len(top)} of {len(rows)} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
