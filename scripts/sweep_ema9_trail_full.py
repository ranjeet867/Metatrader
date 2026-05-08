#!/usr/bin/env python3
"""
sweep_ema9_trail_full.py — comprehensive variant sweep across all
metals + indices, with experiment-log persistence.

Variants tested:
  V1 baseline                — 9-EMA, prev-bar-low trail
  V2 + 50-EMA filter
  V3 loosened 2-bar trail
  V4 50-EMA + 2-bar trail (the V4 winner from prior run)
  V5 W1 baseline (resampled)
  V6 W1 + 50-EMA
  V7 V4 + RSI(14)>55 filter  — for indices (regime-strength gate)
  V8 V4 + RSI>55 + 5y window — gold-specific 5y test

All results persist to data/experiment_log.json so subsequent runs
can skip already-tested combos and build a leaderboard over time.

Usage:
  python scripts/sweep_ema9_trail_full.py
  python scripts/sweep_ema9_trail_full.py --force  # re-test everything
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core.data import load_parquet
from core.indicators import ema, atr_wilder, rsi_wilder
from core import cost_defaults, experiment_log


# Universe — metals + indices
METALS = [
    ("XAUUSD",     100.0,    0.30),
    ("XAGUSD",     5_000.0,  0.40),
    ("XPDUSD",     100.0,    0.20),
    ("XPTUSD",     100.0,    0.25),
]
INDICES = [
    ("US100.cash", 1.0,    6.5),
    ("US500.cash", 1.0,    5.0),
    ("US30.cash",  1.0,    3.0),
    ("JP225.cash", 1.0,    2.0),
    ("GER40.cash", 1.0,    3.0),
    ("UK100.cash", 1.0,    4.0),
    ("FRA40.cash", 1.0,    4.0),
]
ALL_TICKERS = METALS + INDICES

LOOKBACK_BARS = 3
STOP_BUFFER_PCT = 0.001
COMMISSION_USD = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE_ATR_FRAC = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC
RSI_THRESHOLD = 55.0
DAYS_5Y = 252 * 5  # ~5 years of D1 bars


@dataclass
class Trade:
    entry_idx: int
    entry_price: float
    initial_stop: float
    exit_idx: int = -1
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    R_multiple: float = 0.0
    bars_held: int = 0
    exit_reason: str = ""


def run_variant(
    df: pd.DataFrame, *, lots: float, mpu: float,
    use_50ema: bool = False,
    trail_lookback: int = 1,
    use_rsi_gate: bool = False,
) -> list[Trade]:
    """One backtest with the specified flags."""
    n = len(df)
    if n < 60:
        return []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    ema9_vals = ema(df["close"], 9).values
    ema50_vals = ema(df["close"], 50).values
    rsi_vals = rsi_wilder(df["close"], 14).values
    atr_vals = atr_wilder(df, 14).values

    trades: list[Trade] = []
    in_pos = False
    cur: Optional[Trade] = None
    bar_idx = 60  # generous warmup

    while bar_idx < n:
        if not in_pos:
            i = bar_idx
            cond1 = all(
                closes[i - k] > ema9_vals[i - k]
                for k in (1, 2, 3)
                if not np.isnan(ema9_vals[i - k])
            )
            if use_50ema:
                cond1 = cond1 and all(
                    closes[i - k] > ema50_vals[i - k]
                    for k in (1, 2, 3)
                    if not np.isnan(ema50_vals[i - k])
                )
            if use_rsi_gate:
                # Last 3 bars all had RSI > threshold
                cond1 = cond1 and all(
                    rsi_vals[i - k] > RSI_THRESHOLD
                    for k in (1, 2, 3)
                    if not np.isnan(rsi_vals[i - k])
                )
            if not cond1:
                bar_idx += 1
                continue
            three_day_high = max(highs[i - 1], highs[i - 2], highs[i - 3])
            if closes[i] <= three_day_high:
                bar_idx += 1
                continue
            three_day_low = min(lows[i - 1], lows[i - 2], lows[i - 3])
            stop_price = three_day_low * (1.0 - STOP_BUFFER_PCT)
            slip_abs = atr_vals[i] * SLIPPAGE_ATR_FRAC
            entry_price = closes[i] + slip_abs
            cur = Trade(entry_idx=i, entry_price=entry_price,
                          initial_stop=stop_price)
            in_pos = True
            bar_idx += 1
        else:
            assert cur is not None
            j = bar_idx
            tl = max(0, j - trail_lookback)
            trail_low = min(lows[tl:j]) if j > tl else lows[j - 1]
            exit_price = None
            exit_reason = ""
            if lows[j] <= cur.initial_stop:
                exit_price = cur.initial_stop
                exit_reason = "initial_stop"
            elif opens[j] < trail_low:
                exit_price = opens[j]
                exit_reason = "gap_below_trail"
            elif closes[j] < trail_low:
                exit_price = closes[j]
                exit_reason = "close_below_trail"
            if exit_price is not None:
                slip_abs = atr_vals[j] * SLIPPAGE_ATR_FRAC
                exit_price -= slip_abs
                price_move = exit_price - cur.entry_price
                pnl = price_move * lots * mpu - 2 * COMMISSION_USD
                risk_per_unit = cur.entry_price - cur.initial_stop
                risk_dollars = risk_per_unit * lots * mpu
                R = pnl / risk_dollars if risk_dollars > 0 else 0.0
                cur.exit_idx = j
                cur.exit_price = exit_price
                cur.realized_pnl = pnl
                cur.R_multiple = R
                cur.bars_held = j - cur.entry_idx
                cur.exit_reason = exit_reason
                trades.append(cur)
                in_pos = False
                cur = None
            bar_idx += 1
    return trades


def stats_dict(trades: list[Trade]) -> dict:
    if not trades:
        return dict(n=0, pf=0.0, wr=0.0, mean_R=0.0, rr=0.0,
                       max_dd_pct=0.0, avg_hold=0.0)
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    gp = sum(t.realized_pnl for t in wins)
    gl = -sum(t.realized_pnl for t in losses)
    pf = (gp / gl) if gl > 0 else (9.99 if gp > 0 else 0.0)
    wr = len(wins) / len(trades) * 100
    mean_R = sum(t.R_multiple for t in trades) / len(trades)
    avg_w = sum(t.R_multiple for t in wins) / len(wins) if wins else 0
    avg_l = -sum(t.R_multiple for t in losses) / len(losses) if losses else 0
    rr = avg_w / avg_l if avg_l > 0 else 0
    eq = [0.0]
    for t in trades:
        eq.append(eq[-1] + t.realized_pnl)
    eq_arr = pd.Series(eq)
    peak = eq_arr.cummax()
    dd_dollars = float((peak - eq_arr).max())
    max_dd_pct = dd_dollars / 100_000 * 100
    avg_hold = sum(t.bars_held for t in trades) / len(trades)
    return dict(
        n=len(trades), pf=round(pf, 3), wr=round(wr, 1),
        mean_R=round(mean_R, 3), rr=round(rr, 2),
        max_dd_pct=round(max_dd_pct, 2),
        avg_hold=round(avg_hold, 1),
    )


def resample_d1_to_w1(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time")
    w = df.resample("W-FRI", label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    return w.reset_index()


VARIANTS = [
    # (name, params dict, applies-to-tf)
    ("V1_baseline",
     dict(use_50ema=False, trail_lookback=1, use_rsi_gate=False), "D1"),
    ("V2_50ema",
     dict(use_50ema=True, trail_lookback=1, use_rsi_gate=False), "D1"),
    ("V3_2bar_trail",
     dict(use_50ema=False, trail_lookback=2, use_rsi_gate=False), "D1"),
    ("V4_50ema_2bar",
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=False), "D1"),
    ("V7_V4_rsi55",
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=True), "D1"),
    ("V5_W1_baseline",
     dict(use_50ema=False, trail_lookback=1, use_rsi_gate=False), "W1"),
    ("V6_W1_50ema",
     dict(use_50ema=True, trail_lookback=1, use_rsi_gate=False), "W1"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                       help="re-test even if log has the cell")
    ap.add_argument("--last-5y", action="store_true",
                       help="use only last 5 years of bars (gold focus)")
    args = ap.parse_args()

    log = experiment_log.load()
    print(f"Loaded experiment log: {len(log.records)} prior records")
    print()

    new_runs = 0
    skipped = 0
    for ticker, mpu, lots in ALL_TICKERS:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists():
            continue
        df_full = load_parquet(ppath)
        # Optional 5-year window
        if args.last_5y and len(df_full) > DAYS_5Y:
            df_full = df_full.tail(DAYS_5Y).reset_index(drop=True)

        for variant_name, params, tf_target in VARIANTS:
            tf = tf_target
            # Allow re-running with 5y suffix if --last-5y
            label = f"{variant_name}_5y" if args.last_5y else variant_name
            if not args.force and log.has(
                strategy="ema9_trail", variant=label,
                ticker=ticker, tf=tf,
            ):
                skipped += 1
                continue

            if tf == "W1":
                df = resample_d1_to_w1(df_full)
            else:
                df = df_full

            try:
                trades = run_variant(df, lots=lots, mpu=mpu, **params)
                s = stats_dict(trades)
                log.record(
                    strategy="ema9_trail", variant=label,
                    ticker=ticker, tf=tf,
                    stats=s, params=params,
                )
                new_runs += 1
            except Exception as e:
                print(f"  err {ticker} {tf} {label}: {e}")

    log.save()
    print(f"New runs: {new_runs}  ·  Cached skipped: {skipped}")
    print(f"Log saved to: {log.path}  ({len(log.records)} records total)")
    print()

    # ── Leaderboard ───────────────────────────────────────────────
    print("=" * 110)
    print("DEPLOY-SAFE CELLS (PF ≥ 1.05, n ≥ 15, mean_R > 0, DD ≤ 30%)")
    print("=" * 110)
    safe = [r for r in log.records.values()
              if r.strategy == "ema9_trail" and r.deploy_safe]
    safe.sort(key=lambda r: -(float(r.stats.get("pf", 0))
                                * float(r.stats.get("mean_R", 0))))
    print(f"{'Variant':25} {'Ticker':12} {'TF':4} {'n':>4} "
          f"{'PF':>5} {'WR%':>4} {'R:R':>5} {'mean_R':>7} "
          f"{'DD%':>5} {'hold':>5}")
    print("-" * 100)
    for r in safe[:30]:
        s = r.stats
        print(f"{r.variant[:25]:25} {r.ticker[:12]:12} {r.tf:4} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('wr',0)):>3.0f}% {float(s.get('rr',0)):>5.2f} "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}% "
              f"{float(s.get('avg_hold',0)):>4.1f}d")

    if not safe:
        print("(no deploy-safe cells in this experiment)")

    # ── What FAILED — quick view of top "almost made it" ─────────
    print()
    print("=" * 110)
    print("INSTRUMENT × VARIANT MATRIX (D1 only)")
    print("=" * 110)
    d1_recs = [r for r in log.records.values()
                if r.strategy == "ema9_trail" and r.tf == "D1"]
    by_ticker: dict[str, dict[str, str]] = {}
    for r in d1_recs:
        by_ticker.setdefault(r.ticker, {})[r.variant] = (
            f"{'✓' if r.deploy_safe else '✗'} "
            f"PF{float(r.stats.get('pf',0)):.2f} "
            f"R{float(r.stats.get('mean_R',0)):+.2f}"
        )
    d1_variants = [v for v, _, tf in VARIANTS if tf == "D1"]
    print(f"{'Ticker':12} | "
          + " | ".join(f"{v[:14]:>14}" for v in d1_variants))
    print("-" * 12 + "-+-" + "-+-".join("-" * 14 for _ in d1_variants))
    for ticker, _, _ in ALL_TICKERS:
        cells = by_ticker.get(ticker, {})
        if not cells:
            continue
        line = f"{ticker[:12]:12} | "
        line += " | ".join(
            f"{cells.get(v, '—'):>14}" for v in d1_variants
        )
        print(line)


if __name__ == "__main__":
    main()
