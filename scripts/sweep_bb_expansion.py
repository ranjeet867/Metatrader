#!/usr/bin/env python3
"""
sweep_bb_expansion.py — Bollinger Band squeeze-then-expansion strategy.

Mechanism:
  1. Compute BB(20, 2.0): middle = SMA(20), upper/lower = ± 2σ
  2. Compute BB width = (upper − lower) / middle  (as a fraction)
  3. Squeeze detected when width is in bottom 20% of last 100 bars
  4. Entry (long): after squeeze in last `squeeze_window` bars, close
     breaks above upper band → BUY
  5. Entry (short): after squeeze, close breaks below lower band → SELL
  6. Stop: opposite band at entry (e.g. long entry → stop at lower band)
  7. Trail: close < prior `trail_lookback` bars' low (long) — same
     trailing logic as ema9_trail for comparability

Variants:
  V1  long-only, no extra filter
  V2  long + 50-EMA filter (only enter when close > 50-EMA)
  V3  bidirectional (long + short)
  V4  bidir + 50-EMA regime gate

Universe: metals (4) + indices (7) on D1 + H1
Saves all results to data/experiment_log.json under strategy="bb_expansion".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core import cost_defaults, experiment_log
from core.data import load_parquet
from core.indicators import ema, atr_wilder

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
ALL = METALS + INDICES

BB_PERIOD = 20
BB_K = 2.0
WIDTH_LOOKBACK = 100
SQUEEZE_PCTILE = 0.20
SQUEEZE_RECENT = 5         # squeeze must have happened in last 5 bars
COMMISSION = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC


def run_bb_expansion(df: pd.DataFrame, *, lots: float, mpu: float,
                       use_50ema: bool = False,
                       bidirectional: bool = False,
                       trail_lookback: int = 1) -> list[dict]:
    n = len(df)
    if n < 150:
        return []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    atr = atr_wilder(df, 14).values

    # BB
    s = pd.Series(closes)
    middle = s.rolling(BB_PERIOD).mean().values
    std = s.rolling(BB_PERIOD).std().values
    upper = middle + BB_K * std
    lower = middle - BB_K * std
    width = (upper - lower) / np.where(middle != 0, middle, 1.0)

    # Squeeze threshold — rolling 20%-quantile of width
    width_s = pd.Series(width)
    squeeze_threshold = width_s.rolling(WIDTH_LOOKBACK).quantile(
        SQUEEZE_PCTILE).values
    in_squeeze = width <= squeeze_threshold

    ema50 = ema(df["close"], 50).values

    trades: list[dict] = []
    in_pos = False
    cur: Optional[dict] = None
    bar_idx = WIDTH_LOOKBACK + 5

    while bar_idx < n:
        if not in_pos:
            i = bar_idx
            if np.isnan(upper[i]) or np.isnan(lower[i]):
                bar_idx += 1; continue
            # Was there a squeeze in the last `SQUEEZE_RECENT` bars?
            recent_squeeze = any(
                in_squeeze[max(0, i - SQUEEZE_RECENT):i + 1]
            )
            if not recent_squeeze:
                bar_idx += 1; continue

            # Long break-out
            went_long = False
            if (closes[i] > upper[i]
                    and closes[i - 1] <= upper[i - 1]):
                if use_50ema and not (closes[i] > ema50[i]):
                    bar_idx += 1; continue
                slip = atr[i] * SLIPPAGE
                entry = closes[i] + slip
                stop = lower[i]
                if stop > 0 and stop < entry:
                    cur = {"dir": +1, "entry_idx": i, "entry": entry,
                            "stop": stop}
                    in_pos = True
                    went_long = True
            # Short break-out
            if not went_long and bidirectional:
                if (closes[i] < lower[i]
                        and closes[i - 1] >= lower[i - 1]):
                    if use_50ema and not (closes[i] < ema50[i]):
                        bar_idx += 1; continue
                    slip = atr[i] * SLIPPAGE
                    entry = closes[i] - slip
                    stop = upper[i]
                    if stop > entry:
                        cur = {"dir": -1, "entry_idx": i, "entry": entry,
                                "stop": stop}
                        in_pos = True
            bar_idx += 1
        else:
            j = bar_idx
            tl = max(0, j - trail_lookback)
            d = cur["dir"]
            if d == +1:
                trail = min(lows[tl:j]) if j > tl else lows[j - 1]
                xp = None
                if lows[j] <= cur["stop"]:
                    xp = cur["stop"]
                elif opens[j] < trail:
                    xp = opens[j]
                elif closes[j] < trail:
                    xp = closes[j]
                if xp is not None:
                    xp -= atr[j] * SLIPPAGE
                    pnl = (xp - cur["entry"]) * lots * mpu - 2 * COMMISSION
                    risk = (cur["entry"] - cur["stop"]) * lots * mpu
                    R = pnl / risk if risk > 0 else 0
                    cur.update(exit_idx=j, pnl=pnl, R=R,
                                  bars=j - cur["entry_idx"])
                    trades.append(cur)
                    in_pos = False
                    cur = None
            else:  # SHORT
                trail = max(highs[tl:j]) if j > tl else highs[j - 1]
                xp = None
                if highs[j] >= cur["stop"]:
                    xp = cur["stop"]
                elif opens[j] > trail:
                    xp = opens[j]
                elif closes[j] > trail:
                    xp = closes[j]
                if xp is not None:
                    xp += atr[j] * SLIPPAGE
                    pnl = (cur["entry"] - xp) * lots * mpu - 2 * COMMISSION
                    risk = (cur["stop"] - cur["entry"]) * lots * mpu
                    R = pnl / risk if risk > 0 else 0
                    cur.update(exit_idx=j, pnl=pnl, R=R,
                                  bars=j - cur["entry_idx"])
                    trades.append(cur)
                    in_pos = False
                    cur = None
            bar_idx += 1
    return trades


def stats_dict(trades):
    if not trades:
        return dict(n=0, pf=0.0, wr=0.0, mean_R=0.0, max_dd_pct=0.0,
                       gain=0.0, avg_hold=0.0)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gp = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    pf = gp / gl if gl > 0 else (9.99 if gp > 0 else 0)
    eq = [0.0]
    for t in trades:
        eq.append(eq[-1] + t["pnl"])
    eq_arr = pd.Series(eq)
    peak = eq_arr.cummax()
    dd = float((peak - eq_arr).max())
    return dict(
        n=len(trades),
        pf=round(pf, 3),
        wr=round(len(wins) / len(trades) * 100, 1),
        mean_R=round(sum(t["R"] for t in trades) / len(trades), 3),
        gain=round(sum(t["pnl"] for t in trades), 0),
        max_dd_pct=round(dd / 100_000 * 100, 2),
        avg_hold=round(sum(t["bars"] for t in trades) / len(trades), 1),
    )


VARIANTS = [
    ("V1_long_only",      dict(use_50ema=False, bidirectional=False)),
    ("V2_long_50ema",     dict(use_50ema=True, bidirectional=False)),
    ("V3_bidir",          dict(use_50ema=False, bidirectional=True)),
    ("V4_bidir_50ema",    dict(use_50ema=True, bidirectional=True)),
]


def main():
    log = experiment_log.load()
    print(f"Loaded log: {len(log.records)} prior records")

    new = 0
    for ticker, mpu, lots in ALL:
        for tf in ("D1", "H1"):
            ppath = ROOT / "data" / f"{ticker}_{tf}.parquet"
            if not ppath.exists(): continue
            df = load_parquet(ppath)
            if len(df) < 200: continue
            for vname, params in VARIANTS:
                if log.has(strategy="bb_expansion", variant=vname,
                              ticker=ticker, tf=tf):
                    continue
                try:
                    trades = run_bb_expansion(
                        df, lots=lots, mpu=mpu, **params)
                    s = stats_dict(trades)
                    log.record(
                        strategy="bb_expansion", variant=vname,
                        ticker=ticker, tf=tf,
                        stats=s, params=params,
                    )
                    new += 1
                except Exception as e:
                    print(f"err {ticker} {tf} {vname}: {e}")
    log.save()
    print(f"New runs: {new}")
    print()

    # Show deploy-safe BB cells
    bb_records = [r for r in log.records.values()
                    if r.strategy == "bb_expansion"]
    safe = [r for r in bb_records if r.deploy_safe]
    safe.sort(key=lambda r: -float(r.stats.get("pf", 0))
                * float(r.stats.get("mean_R", 0)))

    print("=" * 110)
    print(f"DEPLOY-SAFE BB_EXPANSION CELLS ({len(safe)})")
    print("=" * 110)
    print(f"{'Variant':18} {'Ticker':12} {'TF':4} {'n':>4} "
          f"{'PF':>5} {'WR%':>4} {'mean_R':>7} {'gain$':>10} "
          f"{'DD%':>5} {'hold':>5}")
    for r in safe[:30]:
        s = r.stats
        print(f"{r.variant[:18]:18} {r.ticker[:12]:12} {r.tf:4} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('wr',0)):>3.0f}% "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"${float(s.get('gain',0)):>+9,.0f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}% "
              f"{float(s.get('avg_hold',0)):>4.1f}")
    if not safe:
        print("(none)")

    # Combined leaderboard across BOTH strategies
    print()
    print("=" * 110)
    print("COMBINED LEADERBOARD — top 20 cells across ema9_trail + bb_expansion")
    print("=" * 110)
    all_safe = [r for r in log.records.values() if r.deploy_safe]
    all_safe.sort(
        key=lambda r: -(float(r.stats.get("pf", 0))
                          * float(r.stats.get("mean_R", 0))))
    print(f"{'Strategy':14} {'Variant':22} {'Ticker':12} {'TF':4} "
          f"{'n':>4} {'PF':>5} {'mean_R':>7} {'DD%':>5}")
    for r in all_safe[:20]:
        s = r.stats
        print(f"{r.strategy[:14]:14} {r.variant[:22]:22} "
              f"{r.ticker[:12]:12} {r.tf:4} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}%")


if __name__ == "__main__":
    main()
