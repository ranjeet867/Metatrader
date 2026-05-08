#!/usr/bin/env python3
"""
backtest_ema9_trail_variants.py — test 4 variants of the user's
9-EMA trail strategy to find which combo deploy_safes on
indices + metals.

Variants:
  V1 — baseline (D1, 9-EMA filter, prev-day-low trail)
  V2 — D1 + 50-EMA filter (entry needs close > 9-EMA AND close > 50-EMA)
  V3 — D1 + loosened trail (exit on close < 2-day low)
  V4 — D1 + 50-EMA filter + loosened trail (combo)
  V5 — Weekly TF (resampled from D1) + 9-EMA filter + prev-week-low trail
  V6 — Weekly TF + 50-EMA filter
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core.data import load_parquet
from core.indicators import ema, atr_wilder
from core import cost_defaults

TICKERS = [
    ("US100.cash", 1.0,    6.5),
    ("US500.cash", 1.0,    5.0),
    ("XAUUSD",     100.0,  0.30),
    ("XAGUSD",     5_000.0, 0.40),
]

LOOKBACK_BARS = 3
STOP_BUFFER_PCT = 0.001
COMMISSION_USD = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE_ATR_FRAC = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC


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
    use_50ema: bool, loosen_trail: bool,
    trail_lookback: int = 1,  # 1 = prev bar low, 2 = 2-bar low
) -> list[Trade]:
    """One backtest with the specified variant flags."""
    n = len(df)
    if n < 60:
        return []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    ema9_vals = ema(df["close"], 9).values
    ema50_vals = ema(df["close"], 50).values
    atr_vals = atr_wilder(df, 14).values

    trades: list[Trade] = []
    in_pos = False
    cur: Optional[Trade] = None
    bar_idx = max(LOOKBACK_BARS, 50, 14) + 5  # warmup

    while bar_idx < n:
        if not in_pos:
            i = bar_idx
            if i < 3:
                bar_idx += 1
                continue
            # Condition 1: last 3 closes > 9-EMA
            cond1 = all(
                closes[i - k] > ema9_vals[i - k]
                for k in (1, 2, 3)
                if not np.isnan(ema9_vals[i - k])
            )
            # Condition 1b (variant): also require close > 50-EMA
            if use_50ema:
                cond1 = cond1 and all(
                    closes[i - k] > ema50_vals[i - k]
                    for k in (1, 2, 3)
                    if not np.isnan(ema50_vals[i - k])
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
            # Trail level: min of last `trail_lookback` lows (prior to j)
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


def stats(trades: list[Trade]) -> dict:
    if not trades:
        return dict(n=0, pf=0, wr=0, mean_R=0, rr=0, max_dd_pct=0)
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
    dd_dollars = (peak - eq_arr).max()
    starting_balance = 100_000
    max_dd_pct = dd_dollars / starting_balance * 100
    return dict(n=len(trades), pf=pf, wr=wr, mean_R=mean_R, rr=rr,
                  max_dd_pct=max_dd_pct)


def resample_d1_to_w1(df: pd.DataFrame) -> pd.DataFrame:
    """Resample D1 OHLCV to W1 (Mon → Sun, label-right)."""
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time")
    w = df.resample("W-FRI", label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna()
    w = w.reset_index().rename(columns={"time": "time"})
    return w


def deploy_safe(s: dict) -> bool:
    return s["n"] >= 15 and s["pf"] >= 1.05 and s["mean_R"] > 0


def main():
    variants = [
        ("V1 baseline (9-EMA, prev-day-low trail)",
         dict(use_50ema=False, loosen_trail=False, trail_lookback=1)),
        ("V2 + 50-EMA filter",
         dict(use_50ema=True, loosen_trail=False, trail_lookback=1)),
        ("V3 loosened trail (2-bar low)",
         dict(use_50ema=False, loosen_trail=True, trail_lookback=2)),
        ("V4 50-EMA + 2-bar trail (combo)",
         dict(use_50ema=True, loosen_trail=True, trail_lookback=2)),
    ]

    print("=" * 110)
    print("EMA9 TRAIL — variant comparison on D1")
    print("=" * 110)

    # Print header
    print(f"\n{'Ticker':12} | "
          + " | ".join(f"{n[:25]:>25}" for n, _ in variants))
    print("-" * 12 + "-+-" + "-+-".join("-" * 25 for _ in variants))

    safe_cells = []
    for ticker, mpu, lots in TICKERS:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists():
            continue
        df = load_parquet(ppath)
        cells = []
        for vname, kw in variants:
            trades = run_variant(df, lots=lots, mpu=mpu, **kw)
            s = stats(trades)
            mark = "✓" if deploy_safe(s) else "✗"
            cell = (f"{mark} n={s['n']:>3} PF{s['pf']:>4.2f} "
                    f"R{s['mean_R']:>+5.2f} DD{s['max_dd_pct']:>4.1f}%")
            cells.append(cell)
            if deploy_safe(s):
                safe_cells.append((vname, ticker, "D1", s))
        print(f"{ticker[:12]:12} | " + " | ".join(f"{c:>25}" for c in cells))

    # Weekly TF — resample D1 → W1
    print()
    print("=" * 110)
    print("EMA9 TRAIL — Weekly TF (resampled from D1)")
    print("=" * 110)
    weekly_variants = [
        ("V5 W1 baseline (9-EMA, prev-week-low trail)",
         dict(use_50ema=False, loosen_trail=False, trail_lookback=1)),
        ("V6 W1 + 50-EMA filter",
         dict(use_50ema=True, loosen_trail=False, trail_lookback=1)),
    ]
    print(f"\n{'Ticker':12} | "
          + " | ".join(f"{n[:30]:>30}" for n, _ in weekly_variants))
    print("-" * 12 + "-+-" + "-+-".join("-" * 30 for _ in weekly_variants))

    for ticker, mpu, lots in TICKERS:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists():
            continue
        df = load_parquet(ppath)
        df_w = resample_d1_to_w1(df)
        if len(df_w) < 60:
            continue
        cells = []
        for vname, kw in weekly_variants:
            trades = run_variant(df_w, lots=lots, mpu=mpu, **kw)
            s = stats(trades)
            mark = "✓" if deploy_safe(s) else "✗"
            cell = (f"{mark} n={s['n']:>3} PF{s['pf']:>4.2f} "
                    f"R{s['mean_R']:>+5.2f} DD{s['max_dd_pct']:>4.1f}%")
            cells.append(cell)
            if deploy_safe(s):
                safe_cells.append((vname, ticker, "W1", s))
        print(f"{ticker[:12]:12} | " + " | ".join(f"{c:>30}" for c in cells))

    # Summary
    print()
    print("=" * 110)
    if safe_cells:
        print(f"DEPLOY-SAFE CELLS ({len(safe_cells)}):")
        print(f"  {'Variant':45} {'Ticker':12} {'TF':4} "
              f"{'n':>4} {'PF':>5} {'R:R':>5} {'mean_R':>7} {'DD%':>5}")
        print("  " + "-" * 100)
        for vname, ticker, tf, s in sorted(safe_cells,
                                                key=lambda x: -x[3]["pf"]):
            print(f"  {vname[:45]:45} {ticker[:12]:12} {tf:4} "
                  f"{s['n']:>4} {s['pf']:>5.2f} {s['rr']:>5.2f} "
                  f"{s['mean_R']:>+7.2f} {s['max_dd_pct']:>4.1f}%")
    else:
        print("No deploy-safe cells across any variant.")


if __name__ == "__main__":
    main()
