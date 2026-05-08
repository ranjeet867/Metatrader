#!/usr/bin/env python3
"""sweep_support_resistance.py — pivot-based support/resistance bounce
strategy with 5 confirmation-filter variants.

S/R level detection:
  • Pivot lows/highs from 20-bar windows (look for 10 bars on each side
    that didn't break the level → that's a confirmed pivot)
  • Keep the last 5 levels per side (rolling memory)
  • A level is "active" until price closes through it by > 0.3% of price

Setup (LONG only — bounce off support):
  1. Bar low touches within `touch_pct` of any active support level
  2. Close > open (rejection candle)
  3. Confirmation filter (per variant — see below)
  Stop = level − 0.5 × ATR
  Target = entry + rr_target × stop_distance (R:R 1:1, 1:2, 1:3, trail)

Confirmation variants:
  V1 plain        — no extra filter (just touch + bounce)
  V2 rsi          — RSI(14) < 35 at touch (oversold for the bounce)
  V3 ema_filter   — close > 200-EMA (only fade DOWN moves in uptrend)
  V4 volume       — touch bar volume > 1.5 × 20-bar average
  V5 bb_lower     — bar low pierces lower Bollinger band (vol-confirmed)

Tested on metals (4) + indices (7) on D1 + H1.
Saves to data/experiment_log.json under strategy="support_resistance".
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core import cost_defaults, experiment_log
from core.data import load_parquet
from core.indicators import ema, atr_wilder, rsi_wilder

TICKERS = [
    ("XAUUSD",     100.0,    0.30),
    ("XAGUSD",     5_000.0,  0.40),
    ("XPDUSD",     100.0,    0.20),
    ("XPTUSD",     100.0,    0.25),
    ("US100.cash", 1.0,      6.5),
    ("US500.cash", 1.0,      5.0),
    ("US30.cash",  1.0,      3.0),
    ("JP225.cash", 1.0,      2.0),
    ("GER40.cash", 1.0,      3.0),
    ("UK100.cash", 1.0,      4.0),
    ("FRA40.cash", 1.0,      4.0),
]
COMMISSION = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC

PIVOT_LOOKBACK = 10           # bars on each side to confirm a pivot
KEEP_LEVELS = 5               # rolling memory of last N levels
TOUCH_PCT = 0.003             # within 0.3% of level
LEVEL_BREAK_PCT = 0.003       # close past by > 0.3% invalidates level
STOP_ATR_PAD = 0.5
RSI_OS = 35
EMA_TREND = 200
VOL_MULT = 1.5
BB_PERIOD = 20
BB_K = 2.0


def _find_pivot_lows(lows: np.ndarray, k: int) -> list[int]:
    """Return indices of confirmed pivot lows (bar with `k` higher
    lows on each side)."""
    out = []
    for i in range(k, len(lows) - k):
        window = lows[i - k:i + k + 1]
        if lows[i] == window.min() and (window == lows[i]).sum() == 1:
            out.append(i)
    return out


def run_sr(df: pd.DataFrame, *, lots: float, mpu: float,
            variant: str, rr_target: float = 2.0) -> list[dict]:
    n = len(df)
    if n < 250:
        return []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    atr = atr_wilder(df, 14).values

    # Indicators per variant
    if variant == "rsi":
        rsi = rsi_wilder(df["close"], 14).values
    if variant == "ema_filter":
        e200 = ema(df["close"], EMA_TREND).values
    if variant == "volume":
        if "tick_volume" in df.columns:
            vol = df["tick_volume"].values.astype(float)
        elif "volume" in df.columns:
            vol = df["volume"].values.astype(float)
        else:
            vol = (df["high"] - df["low"]).values.astype(float)
        vol_avg = pd.Series(vol).rolling(20).mean().values
    if variant == "bb_lower":
        s = pd.Series(closes)
        bb_mid = s.rolling(BB_PERIOD).mean().values
        bb_std = s.rolling(BB_PERIOD).std().values
        bb_lo = bb_mid - BB_K * bb_std

    # Pre-compute pivot lows once
    pivots = _find_pivot_lows(lows, PIVOT_LOOKBACK)
    # For each bar, build the "active levels" up to that bar
    active_levels: list[float] = []
    pivot_idx_ptr = 0

    trades: list[dict] = []
    in_pos = False
    cur: Optional[dict] = None

    for i in range(250, n):
        # Promote any newly-confirmed pivots that are at least k bars back
        while (pivot_idx_ptr < len(pivots)
               and pivots[pivot_idx_ptr] + PIVOT_LOOKBACK <= i):
            new_lvl = float(lows[pivots[pivot_idx_ptr]])
            active_levels.append(new_lvl)
            if len(active_levels) > KEEP_LEVELS:
                active_levels.pop(0)
            pivot_idx_ptr += 1
        # Drop levels broken by recent close
        active_levels = [
            lv for lv in active_levels
            if closes[i - 1] > lv * (1 - LEVEL_BREAK_PCT)
        ]

        if not in_pos:
            if not active_levels or atr[i] <= 0:
                continue
            # Find the closest level the bar's low touched
            touched_lvl = None
            for lv in active_levels:
                if abs(lows[i] - lv) / lv < TOUCH_PCT \
                        or (lows[i] <= lv <= highs[i]):
                    touched_lvl = lv
                    break
            if touched_lvl is None:
                continue
            # Rejection candle
            if not (closes[i] > opens[i]):
                continue

            # Variant-specific filter
            if variant == "rsi":
                if not (rsi[i] < RSI_OS):
                    continue
            elif variant == "ema_filter":
                if not (closes[i] > e200[i]):
                    continue
            elif variant == "volume":
                if not (vol[i] > VOL_MULT * (vol_avg[i] or 1)):
                    continue
            elif variant == "bb_lower":
                if not (lows[i] <= bb_lo[i]):
                    continue
            # variant == "plain" → no extra check

            stop = touched_lvl - STOP_ATR_PAD * atr[i]
            if stop <= 0 or stop >= closes[i]:
                continue
            slip = atr[i] * SLIPPAGE
            entry = closes[i] + slip
            risk = entry - stop
            target = entry + rr_target * risk
            cur = {"dir": +1, "entry_idx": i, "entry": entry,
                   "stop": stop, "target": target,
                   "risk_per_unit": risk}
            in_pos = True
        else:
            j = i
            xp = None
            if lows[j] <= cur["stop"]:
                xp = cur["stop"]
            elif highs[j] >= cur["target"]:
                xp = cur["target"]
            if xp is not None:
                if xp != cur["target"]:
                    xp -= atr[j] * SLIPPAGE
                pnl = (xp - cur["entry"]) * lots * mpu - 2 * COMMISSION
                R = pnl / (cur["risk_per_unit"] * lots * mpu) \
                    if cur["risk_per_unit"] > 0 else 0
                cur.update(exit_idx=j, pnl=pnl, R=R,
                            bars=j - cur["entry_idx"])
                trades.append(cur)
                in_pos = False
                cur = None
    return trades


def stats(trades):
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


VARIANTS = ["plain", "rsi", "ema_filter", "volume", "bb_lower"]


def main():
    log = experiment_log.load()
    print(f"Loaded log: {len(log.records)} prior records\n")

    new = 0
    for ticker, mpu, lots in TICKERS:
        for tf in ("D1", "H1"):
            ppath = ROOT / "data" / f"{ticker}_{tf}.parquet"
            if not ppath.exists():
                continue
            df = load_parquet(ppath)
            if len(df) < 250:
                continue
            for vname in VARIANTS:
                if log.has(strategy="support_resistance", variant=vname,
                           ticker=ticker, tf=tf):
                    continue
                try:
                    trades = run_sr(df, lots=lots, mpu=mpu,
                                    variant=vname, rr_target=2.0)
                    s = stats(trades)
                    log.record(
                        strategy="support_resistance", variant=vname,
                        ticker=ticker, tf=tf,
                        stats=s,
                        params={"variant": vname, "rr": 2.0,
                                "pivot_k": PIVOT_LOOKBACK},
                    )
                    new += 1
                except Exception as e:
                    print(f"err {ticker} {tf} {vname}: {e}")
    log.save()
    print(f"New runs: {new}\n")

    sr_records = [r for r in log.records.values()
                  if r.strategy == "support_resistance"]
    safe = [r for r in sr_records
            if r.deploy_safe and float(r.stats.get("pf", 0)) > 1.3]
    safe.sort(key=lambda r: -(float(r.stats.get("pf", 0))
                                * max(0.001, float(r.stats.get("mean_R", 0)))))

    print("=" * 110)
    print(f"DEPLOY-SAFE S/R CELLS PF>1.3 ({len(safe)}):")
    print("=" * 110)
    print(f"  {'Variant':12} {'Ticker':12} {'TF':4} {'n':>4} {'PF':>5} "
          f"{'WR%':>4} {'mean_R':>7} {'gain$':>10} {'DD%':>5}")
    for r in safe[:25]:
        s = r.stats
        print(f"  {r.variant[:12]:12} {r.ticker[:12]:12} {r.tf:4} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('wr',0)):>3.0f}% "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"${float(s.get('gain',0)):>+9,.0f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}%")


if __name__ == "__main__":
    main()
