#!/usr/bin/env python3
"""
sweep_rr_winrate.py — find high-win-rate (strategy × ticker × TF) combos
across multiple R:R targets.

Hypothesis the user is testing:
    "Some strategies are naturally HIGH-WR / LOW-R:R (mean-reversion,
     scalp). Others are naturally LOW-WR / HIGH-R:R (trend-following).
     I want to find the cells that hold ≥60% WR at R:R 1:1, and ≥55%
     WR as the R:R stretches up to 1:1.2."

For each (strategy, ticker, TF) combo, we backtest at multiple
target_atr_mult values while keeping stop_atr_mult fixed. The R:R the
table reports is the *intended* R:R = target_atr_mult / stop_atr_mult
(actual realised R:R can differ slightly due to slippage / gaps).

Output:
    data/rr_sweep_results.csv  — full table
    Console — top-10 by win-rate at the user's R:R floor

Usage:
    python scripts/sweep_rr_winrate.py
    python scripts/sweep_rr_winrate.py --rr 1.0 1.1 1.2 1.5 2.0
    python scripts/sweep_rr_winrate.py --tickers US100.cash JP225.cash
    python scripts/sweep_rr_winrate.py --strategies ema_cross donchian_breakout
    python scripts/sweep_rr_winrate.py --min-wr 60 --rr-floor 1.0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.backtest import run_backtest         # noqa: E402
from core.backtest_stats import compute_full_stats   # noqa: E402
from core.data import load_parquet              # noqa: E402


# Ticker → default money-per-unit (point-value × contract-size proxy
# used for $-comparable backtests). Index CFDs are $1/pt/lot, FX is more
# complex but $1/pt is a reasonable equity-curve approximation for the
# hit-rate analysis (we care about WR/expectancy ratios, not absolute $).
DEFAULT_MPU = 1.0


# (strategy_name, factory_fn, base_param_kwargs)
# factory_fn must accept stop_atr_mult, target_atr_mult, long_only kwargs.
# base_param_kwargs is the *other* params you'd hold fixed.
def _build_strategy(name: str, *, stop_mult: float, target_mult: float,
                     long_only: bool):
    """Strategy factory keyed by canonical name. Returns the strategy
    instance OR None if the name isn't supported in the sweep."""
    if name == "ema_cross_9_20":
        from strategies.ema_cross import EmaCross, EmaCrossParams
        return EmaCross(EmaCrossParams(
            fast_period=9, slow_period=20,
            stop_atr_mult=stop_mult, target_atr_mult=target_mult,
            long_only=long_only,
        ))
    if name == "ema_cross_12_26":
        from strategies.ema_cross import EmaCross, EmaCrossParams
        return EmaCross(EmaCrossParams(
            fast_period=12, slow_period=26,
            stop_atr_mult=stop_mult, target_atr_mult=target_mult,
            long_only=long_only,
        ))
    if name == "ema_pullback_20_50":
        # ema_pullback's target is parameterised in R-multiples
        # (target_R_mult = N × stop_distance), so the R:R sweep maps
        # directly to target_R_mult = rr (stop_atr_pad held fixed).
        from strategies.ema_pullback import EmaPullback, EmaPullbackParams
        return EmaPullback(EmaPullbackParams(
            fast_period=20, slow_period=50,
            target_R_mult=target_mult / stop_mult,
            long_only=long_only,
        ))
    if name == "donchian_20":
        from strategies.donchian_breakout import (
            DonchianBreakout, DonchianBreakoutParams,
        )
        return DonchianBreakout(DonchianBreakoutParams(
            period=20,
            stop_atr_mult=stop_mult, target_atr_mult=target_mult,
            long_only=long_only,
        ))
    if name == "donchian_55":
        from strategies.donchian_breakout import (
            DonchianBreakout, DonchianBreakoutParams,
        )
        return DonchianBreakout(DonchianBreakoutParams(
            period=55,
            stop_atr_mult=stop_mult, target_atr_mult=target_mult,
            long_only=long_only,
        ))
    if name == "rsi_30_70":
        from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
        return RsiMeanRev(RsiMeanRevParams(
            oversold=30, overbought=70,
            stop_atr_mult=stop_mult, target_atr_mult=target_mult,
            long_only=long_only,
        ))
    # Note: bbands_meanrev uses the middle band as its natural target
    # (no target_atr_mult), so it's excluded from R:R sweeps. Same for
    # any strategy with a non-ATR-based exit.
    return None


SUPPORTED_STRATEGIES = [
    "ema_cross_9_20",
    "ema_cross_12_26",
    "ema_pullback_20_50",
    "donchian_20",
    "donchian_55",
    "rsi_30_70",
]


@dataclass
class SweepRow:
    strategy: str
    ticker: str
    tf: str
    rr: float
    stop_mult: float
    target_mult: float
    long_only: bool
    n_trades: int
    win_rate_pct: float
    avg_r: float
    expectancy_r: float
    profit_factor: float
    net_pnl_dollars: float
    trades_per_day: float
    # Survival metrics — what makes a real edge vs. noise
    max_consec_losses: int
    max_dd_dollars: float
    max_dd_pct: float
    recovery_days: float | None       # None if never recovered
    sharpe_R: float                    # mean(R) / std(R)
    kelly_pct: float                   # WR - (1-WR)/RR — the math edge
    slip_atr_frac: float               # slippage assumption in this run
    survival_score: float              # composite — see _score()
    # OOS partition
    n_train: int
    n_test: int
    test_win_rate_pct: float
    test_avg_r: float


def _safe_div(a, b):
    return a / b if b else 0.0


def _kelly(win_rate: float, rr: float) -> float:
    """Kelly criterion = WR - (1-WR)/RR.
    Positive ⇒ statistical edge in your favour (worth betting on).
    Negative ⇒ even with great win-rate the R:R doesn't pay enough."""
    if rr <= 0:
        return -1.0
    return win_rate - (1.0 - win_rate) / rr


def _survival_score(*, kelly: float, n_trades: int, pf: float,
                      max_consec_losses: int, max_dd_pct: float,
                      recovery_days: float | None,
                      tpd: float) -> float:
    """Composite 0-100 score that rewards REAL edge and penalises pain.

    User's philosophy (verbatim):
      "10 losing streak is fine AS LONG AS we don't have big loss or
       recovery is fast"

    So we DON'T penalize streak length aggressively — what matters is:
      • the DD-% the streak produces (size of pain)
      • recovery time (how long capital is tied up)

    Hard disqualifiers (instant -score):
      - Negative Kelly → no statistical edge
      - PF < 1.1 → edge eaten by slippage/commission
      - <30 trades → sample too small
      - <0.05 trades/day → signal too rare

    Soft penalties (proportional pain):
      - DD > 8% of starting balance (FTMO buffer to 10% bust = -5/% over)
      - Recovery > 60 days = -0.5 per day over (capital tied up)
      - Recovery NEVER = -50 (still in drawdown at end of data)
      - Max consec losses > 10 = -2/streak (NOT punitive — only matters
        if it produces big DD, which is already penalised above)
    """
    if kelly <= 0:
        return -100.0
    if n_trades < 30:
        return -50.0
    if pf < 1.1:
        return -30.0
    # No frequency floor — D1 strategies are RARE (1-2 trades/month) but
    # often the highest-WR. Don't penalise them for being slow.
    # Base: 100 × kelly × √trades (more trades = more confident sample)
    score = 100.0 * kelly * (n_trades ** 0.5) / 10.0
    # DD pain — the user's "no big loss" criterion
    if max_dd_pct > 8.0:
        score -= 5.0 * (max_dd_pct - 8.0)
    # Recovery pain — but only if the DD was meaningful in the first
    # place. A "never recovered" with a 0.5% DD is not a problem.
    if recovery_days is None and max_dd_pct >= 5.0:
        score -= 30.0
    elif recovery_days is not None and recovery_days > 60:
        score -= 0.5 * (recovery_days - 60)
    # Streak length — only matters past 10, and gently. Real protection
    # comes from DD% (already penalised above).
    if max_consec_losses > 10:
        score -= 2.0 * (max_consec_losses - 10)
    return round(score, 2)


def _backtest_one(*, strategy_name: str, ticker: str, tf: str,
                   stop_mult: float, target_mult: float, long_only: bool,
                   balance: float, lots: float, money_per_unit: float,
                   train_pct: float, slip_atr_frac: float,
                   commission_per_trade: float,
                   auto_mpu: bool = True) -> SweepRow | None:
    parquet = ROOT / "data" / f"{ticker}_{tf}.parquet"
    if not parquet.exists():
        return None
    # Auto-load per-instrument money_per_unit from data/symbol_info.json so
    # XAUUSD / EURUSD / stocks aren't priced as if they were $1/pt indices.
    # money_per_unit = tick_value / tick_size = $ per 1.0 price unit per lot.
    if auto_mpu:
        try:
            from core.symbol_info_loader import try_load
            si = try_load(ticker)
            if si is not None and si.tick_size > 0 and si.tick_value > 0:
                money_per_unit = si.tick_value / si.tick_size
        except Exception:
            pass    # fall through to the passed-in default
    strat = _build_strategy(strategy_name, stop_mult=stop_mult,
                              target_mult=target_mult, long_only=long_only)
    if strat is None:
        return None
    try:
        df = load_parquet(parquet)
    except Exception:
        return None
    if len(df) < 200:
        return None
    try:
        sigs = strat.signals(df)
    except Exception:
        return None
    if len(sigs) < 5:
        return None
    try:
        result = run_backtest(
            df, sigs, starting_balance=balance, lots=lots,
            money_per_unit_price=money_per_unit,
            slippage_per_fill_atr_frac=slip_atr_frac,
            commission_per_trade=commission_per_trade,
        )
    except Exception:
        return None
    trades = result.trades
    if not trades:
        return None

    # Use the canonical stats engine (handles streaks, DD, recovery, etc).
    try:
        stats = compute_full_stats(result, starting_balance=balance)
    except Exception:
        return None

    n = stats.n_trades
    span_days = max(
        1.0,
        (df["time"].iloc[-1] - df["time"].iloc[0]).total_seconds() / 86400.0,
    )
    tpd = n / span_days
    rr = round(target_mult / stop_mult, 2)
    win_rate = stats.win_rate_pct / 100.0
    kelly = _kelly(win_rate, rr)

    # Expectancy per trade in R units
    rs = [float(t.r_multiple) for t in trades]
    win_rs = [r for r in rs if r > 0]
    loss_rs = [r for r in rs if r <= 0]
    expect = (win_rate * (sum(win_rs) / max(1, len(win_rs)))
                + (1 - win_rate) * (sum(loss_rs) / max(1, len(loss_rs))))

    score = _survival_score(
        kelly=kelly, n_trades=n,
        pf=stats.profit_factor if stats.profit_factor != float("inf") else 99.0,
        max_consec_losses=stats.max_consec_losses,
        max_dd_pct=stats.max_dd_pct,
        recovery_days=stats.recovery_duration_days,
        tpd=tpd,
    )

    # OOS partition
    n_bars = len(df)
    split_idx = int(n_bars * train_pct)
    test_trades = [t for t in trades if t.entry_bar_idx >= split_idx]
    n_train = n - len(test_trades)
    n_test = len(test_trades)
    if n_test:
        t_wins = sum(1 for t in test_trades if t.realized_pnl > 0)
        t_rs = [float(t.r_multiple) for t in test_trades]
        t_wr = t_wins / n_test * 100
        t_avg_r = sum(t_rs) / len(t_rs) if t_rs else 0.0
    else:
        t_wr = 0.0
        t_avg_r = 0.0
    return SweepRow(
        strategy=strategy_name, ticker=ticker, tf=tf,
        rr=rr, stop_mult=stop_mult, target_mult=target_mult,
        long_only=long_only,
        n_trades=n,
        win_rate_pct=round(stats.win_rate_pct, 2),
        avg_r=round(stats.avg_R, 4),
        expectancy_r=round(expect, 4),
        profit_factor=round(stats.profit_factor, 3)
            if stats.profit_factor != float("inf") else 99.0,
        net_pnl_dollars=round(stats.sum_realized, 2),
        trades_per_day=round(tpd, 3),
        max_consec_losses=int(stats.max_consec_losses),
        max_dd_dollars=round(stats.max_dd_dollars, 2),
        max_dd_pct=round(stats.max_dd_pct, 2),
        recovery_days=(round(stats.recovery_duration_days, 1)
                          if stats.recovery_duration_days is not None
                          else None),
        sharpe_R=round(stats.sharpe_R, 3),
        kelly_pct=round(kelly * 100, 2),
        slip_atr_frac=slip_atr_frac,
        survival_score=score,
        n_train=n_train, n_test=n_test,
        test_win_rate_pct=round(t_wr, 2),
        test_avg_r=round(t_avg_r, 4),
    )


def _backtest_worker(job: dict) -> SweepRow | None:
    """Top-level multiprocessing worker — must be picklable. Just unpacks
    a job dict and calls _backtest_one."""
    return _backtest_one(**job)


def _discover_combos(*, tickers: list[str] | None, tfs: list[str] | None
                       ) -> list[tuple[str, str]]:
    """Walk data/*.parquet to find every (ticker, tf) combo we have data
    for, optionally filtered."""
    combos = []
    for p in (ROOT / "data").glob("*.parquet"):
        stem = p.stem
        if "_" not in stem:
            continue
        tk, tf = stem.rsplit("_", 1)
        if tickers and tk not in tickers:
            continue
        if tfs and tf not in tfs:
            continue
        combos.append((tk, tf))
    return sorted(set(combos))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rr", nargs="+", type=float,
                     default=[1.0, 1.1, 1.2, 1.5, 2.0],
                     help="Reward:risk multipliers to sweep "
                           "(target_atr_mult / stop_atr_mult). "
                           "Default: 1.0 1.1 1.2 1.5 2.0")
    ap.add_argument("--stop-mult", type=float, default=1.5,
                     help="stop_atr_mult held fixed across the sweep "
                           "(default 1.5; target_mult = rr * stop_mult)")
    ap.add_argument("--strategies", nargs="+", default=None,
                     help=f"Subset to test. Choices: {SUPPORTED_STRATEGIES}")
    ap.add_argument("--tickers", nargs="+", default=None)
    ap.add_argument("--tfs", nargs="+", default=["M15", "H1", "D1"])
    ap.add_argument("--bidir", action="store_true",
                     help="Test bidirectional (long+short) instead of long-only")
    ap.add_argument("--min-wr", type=float, default=55.0,
                     help="Filter floor for the 'good cells' top-N (default 55%)")
    ap.add_argument("--rr-floor", type=float, default=1.0,
                     help="Apply --min-wr only at R:R ≥ this (default 1.0)")
    ap.add_argument("--min-trades", type=int, default=30,
                     help="Drop rows with fewer trades than this (sample size)")
    # Pull defaults from core.cost_defaults — same constants the
    # Backtest page, Composer, run_backtest CLI, sweep_grid, and
    # rebaseline_catalog use. Override at the CLI for what-if runs.
    from core import cost_defaults as _cd
    ap.add_argument("--balance", type=float,
                     default=_cd.DEFAULT_STARTING_BALANCE_USD)
    ap.add_argument("--lots", type=float, default=0.1)
    ap.add_argument("--money-per-unit", type=float, default=DEFAULT_MPU)
    ap.add_argument("--train-pct", type=float,
                     default=_cd.DEFAULT_TRAIN_PCT)
    ap.add_argument("--slip-atr-frac", type=float,
                     default=_cd.DEFAULT_SLIPPAGE_ATR_FRAC,
                     help=f"Per-fill slippage as fraction of ATR. "
                           f"Default {_cd.DEFAULT_SLIPPAGE_ATR_FRAC} "
                           f"(from core.cost_defaults).")
    ap.add_argument("--commission", type=float,
                     default=_cd.DEFAULT_COMMISSION_USD,
                     help=f"$ deducted per round-trip trade. Default "
                           f"${_cd.DEFAULT_COMMISSION_USD:.0f} "
                           f"(from core.cost_defaults).")
    ap.add_argument("--out", default="data/rr_sweep_results.csv")
    ap.add_argument("--workers", type=int, default=2,
                     help="Parallel processes. Default 2 — keeps your "
                           "laptop responsive. Set to 1 for sequential, "
                           "or higher (e.g. cpu_count) for max speed at "
                           f"the cost of CPU heat. You have "
                           f"{os.cpu_count()} cores available.")
    args = ap.parse_args()
    args.workers = max(1, min(args.workers, os.cpu_count() or 2))

    strategies = args.strategies or SUPPORTED_STRATEGIES
    bad = [s for s in strategies if s not in SUPPORTED_STRATEGIES]
    if bad:
        print(f"Unknown strategies: {bad}\nChoices: {SUPPORTED_STRATEGIES}")
        return 2

    combos = _discover_combos(tickers=args.tickers, tfs=args.tfs)
    if not combos:
        print("No (ticker, tf) combos found in data/. Fetch parquets first.")
        return 2

    long_only = not args.bidir
    total = len(strategies) * len(combos) * len(args.rr)
    print(f"\nSweep: {len(strategies)} strategies × {len(combos)} (ticker,tf) "
          f"× {len(args.rr)} R:R values = {total} backtests")
    print(f"  stop_mult={args.stop_mult} · long_only={long_only} · "
          f"min_trades={args.min_trades} · workers={args.workers} "
          f"({'parallel' if args.workers > 1 else 'sequential'})\n")

    # Build the job list
    jobs = []
    for strat in strategies:
        for ticker, tf in combos:
            for rr in args.rr:
                jobs.append(dict(
                    strategy_name=strat, ticker=ticker, tf=tf,
                    stop_mult=args.stop_mult,
                    target_mult=args.stop_mult * rr,
                    long_only=long_only,
                    balance=args.balance, lots=args.lots,
                    money_per_unit=args.money_per_unit,
                    train_pct=args.train_pct,
                    slip_atr_frac=args.slip_atr_frac,
                    commission_per_trade=args.commission,
                ))

    rows: list[SweepRow] = []
    t0 = time.time()
    done = 0

    if args.workers == 1:
        # Sequential — easiest to debug, lowest CPU
        for job in jobs:
            row = _backtest_worker(job)
            done += 1
            if row is not None and row.n_trades >= args.min_trades:
                rows.append(row)
            if done % 25 == 0:
                eta = (time.time() - t0) / done * (total - done)
                print(f"  [{done}/{total}] ({eta:.0f}s remaining) "
                      f"kept={len(rows)}")
    else:
        # Parallel — bounded so the laptop stays usable
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_backtest_worker, job) for job in jobs]
            for fut in as_completed(futures):
                try:
                    row = fut.result()
                except Exception as e:
                    row = None
                    print(f"  worker error: {type(e).__name__}: {e}")
                done += 1
                if row is not None and row.n_trades >= args.min_trades:
                    rows.append(row)
                if done % 25 == 0:
                    eta = (time.time() - t0) / done * (total - done)
                    print(f"  [{done}/{total}] ({eta:.0f}s remaining) "
                          f"kept={len(rows)}")

    print(f"\n✅ Sweep complete in {time.time()-t0:.1f}s. "
          f"{len(rows)}/{total} rows passed --min-trades={args.min_trades}.\n")

    if not rows:
        print("No rows survived. Try lowering --min-trades or widening "
              "ticker/strategy filters.")
        return 1

    df = pd.DataFrame([row.__dict__ for row in rows])
    df = df.sort_values(["rr", "win_rate_pct"], ascending=[True, False])

    # Save full table
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"  Full results → {out_path.relative_to(ROOT)}\n")

    # ----- TOP CELLS BY SURVIVAL SCORE — the real ranking -----
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"TOP 20 CELLS BY SURVIVAL SCORE  "
          f"(slippage={args.slip_atr_frac}×ATR · comm=${args.commission}/trade)")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    by_score = df.sort_values("survival_score", ascending=False)
    cols = ["strategy", "ticker", "tf", "rr",
              "win_rate_pct", "kelly_pct", "profit_factor",
              "max_consec_losses", "max_dd_pct", "recovery_days",
              "n_trades", "trades_per_day", "survival_score"]
    n_positive = (df["survival_score"] > 0).sum()
    print(by_score.head(20)[cols].to_string(index=False))
    if n_positive == 0:
        print(f"\n  ⚠ NONE of the {len(df)} cells passed all survival "
              f"gates (positive Kelly + PF≥1.1 + DD/recovery).")
        print(f"     The above is the LEAST-BAD list. To see pre-cost "
              f"numbers run with --slip-atr-frac 0 --commission 0.")
    else:
        print(f"\n  ✅ {n_positive}/{len(df)} cells PASS all survival gates "
              f"(score > 0). Above are top-20 by score.")

    # ----- TOP CELLS BY USER'S WR FLOOR (legacy view) -----
    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"HIGH-WR FILTER  · WR ≥ {args.min_wr:.0f}% at R:R ≥ {args.rr_floor:.1f}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    elig = df[(df["rr"] >= args.rr_floor)
              & (df["win_rate_pct"] >= args.min_wr)
              & (df["kelly_pct"] > 0)]   # must have real edge
    if elig.empty:
        print(f"  (none — try lower --min-wr or --rr-floor)")
    else:
        cols = ["strategy", "ticker", "tf", "rr",
                  "win_rate_pct", "kelly_pct", "profit_factor",
                  "max_consec_losses", "max_dd_pct",
                  "n_trades", "test_win_rate_pct", "survival_score"]
        top = elig.sort_values(["survival_score", "win_rate_pct"],
                                  ascending=[False, False]).head(20)
        print(top[cols].to_string(index=False))

    # ----- WR DECAY across R:R for the top-by-survival cells -----
    print(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"R:R DECAY: top survival cells, all R:R values side-by-side")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if not by_score.empty:
        # Pick top-5 unique (strategy, ticker, tf) combos
        seen = set()
        top_combos = []
        for _, r in by_score.iterrows():
            key = (r["strategy"], r["ticker"], r["tf"])
            if key not in seen:
                seen.add(key)
                top_combos.append(r)
            if len(top_combos) >= 5:
                break
        decay_rows = []
        for r in top_combos:
            row_dict = {
                "strategy": r["strategy"],
                "ticker":   r["ticker"],
                "tf":       r["tf"],
            }
            for rr in args.rr:
                m = df[(df["strategy"] == r["strategy"])
                         & (df["ticker"] == r["ticker"])
                         & (df["tf"] == r["tf"])
                         & (df["rr"] == rr)]
                if m.empty:
                    row_dict[f"R{rr}"] = "—"
                else:
                    row = m.iloc[0]
                    pf_str = f"PF{row['profit_factor']:.2f}" \
                        if row['profit_factor'] != 99 else "PF∞"
                    row_dict[f"R{rr}"] = (f"WR{row['win_rate_pct']:.0f} "
                                              f"K{row['kelly_pct']:+.1f} "
                                              f"{pf_str}")
            decay_rows.append(row_dict)
        print(pd.DataFrame(decay_rows).to_string(index=False))
        print("\n  (legend: WR=win-rate%, K=Kelly%, PF=profit-factor)")

    # ----- SUMMARY -----
    n_with_edge = (df["kelly_pct"] > 0).sum()
    n_recover = ((df["recovery_days"].notna())
                   & (df["recovery_days"] <= 90)).sum()
    n_max7_streak = (df["max_consec_losses"] <= 7).sum()
    print(f"\nSUMMARY: {len(df)} cells tested, "
          f"{n_with_edge} have positive Kelly, "
          f"{n_recover} recover within 90d, "
          f"{n_max7_streak} have ≤7 consec losses.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
