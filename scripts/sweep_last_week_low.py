#!/usr/bin/env python3
"""
sweep_last_week_low.py — last-week-low reversal strategy with R:R variations.

Mechanism:
  Setup:
    For each D1 bar, compute prior COMPLETED week's low (Mon-Fri).
    Entry triggers when:
      1. Current bar's low touches within 0.5% of prior_week_low (a "test")
      2. Current bar closes higher than open (rejection candle)
      3. (Optional) Higher TF trend filter: 50-EMA > 200-EMA on D1
    Stop = prior_week_low − 0.5 × ATR(14)  (allows for normal noise below)

  Exit variations tested per cell:
    R:R = 1, 2, 3, 4 fixed targets
    "trail" = ride until close < prior_week_low (invalidation of support)

Saves all results to data/experiment_log.json under strategy="last_week_low".
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

from core import cost_defaults, experiment_log
from core.data import load_parquet
from core.indicators import ema, atr_wilder

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


def compute_prior_week_lows(df: pd.DataFrame) -> np.ndarray:
    """For each D1 bar, return the prior completed week's low.
    Friday close → next Monday's prior_week_low = this week's low.
    """
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    df["iso_year"] = df["time"].dt.isocalendar().year
    df["iso_week"] = df["time"].dt.isocalendar().week
    # Group by (iso_year, iso_week), get min low per week
    weekly_low = df.groupby(["iso_year", "iso_week"])["low"].min().reset_index()
    weekly_low.columns = ["iso_year", "iso_week", "week_low"]
    # For each bar, look up its OWN week's low — but we want PRIOR week's low.
    # Strategy: shift week_low by 1 within the weekly_low df, then merge back.
    weekly_low = weekly_low.sort_values(["iso_year", "iso_week"]).reset_index(drop=True)
    weekly_low["prior_week_low"] = weekly_low["week_low"].shift(1)
    df = df.merge(weekly_low[["iso_year", "iso_week", "prior_week_low"]],
                     on=["iso_year", "iso_week"], how="left")
    return df["prior_week_low"].values


@dataclass
class Trade:
    entry_idx: int = 0
    exit_idx: int = 0
    entry_price: float = 0.0
    exit_price: float = 0.0
    initial_stop: float = 0.0
    target_price: float = 0.0
    realized_pnl: float = 0.0
    R_multiple: float = 0.0
    bars_held: int = 0
    exit_reason: str = ""


def run_last_week_low(
    df: pd.DataFrame, *, lots: float, mpu: float,
    target_R: float = 2.0,           # fixed target in R-units
    trail_invalidation: bool = False, # if True, ignore target_R, trail
    use_50_200_filter: bool = True,   # only enter when 50EMA > 200EMA
    touch_pct: float = 0.005,         # test = within 0.5% of prior_week_low
) -> list[Trade]:
    n = len(df)
    if n < 250: return []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    atr = atr_wilder(df, 14).values
    e50 = ema(df["close"], 50).values
    e200 = ema(df["close"], 200).values
    prior_week_low = compute_prior_week_lows(df)

    trades: list[Trade] = []
    in_pos = False
    cur: Optional[Trade] = None
    bar_idx = 250

    while bar_idx < n:
        if not in_pos:
            i = bar_idx
            pwl = prior_week_low[i]
            if pwl is None or np.isnan(pwl):
                bar_idx += 1; continue

            # Setup: bar's low TOUCHED prior_week_low (within touch_pct)
            # AND close > open (rejection)
            touched = abs(lows[i] - pwl) / pwl <= touch_pct
            rejected = closes[i] > opens[i]
            if not (touched and rejected):
                bar_idx += 1; continue

            # Optional regime filter
            if use_50_200_filter:
                if np.isnan(e50[i]) or np.isnan(e200[i]):
                    bar_idx += 1; continue
                if e50[i] <= e200[i]:
                    bar_idx += 1; continue

            # Stop = prior_week_low − 0.5 × ATR
            stop = pwl - 0.5 * atr[i]
            if stop <= 0 or stop >= closes[i]:
                bar_idx += 1; continue

            slip = atr[i] * SLIPPAGE
            entry = closes[i] + slip
            risk_per_unit = entry - stop
            target = entry + target_R * risk_per_unit if not trail_invalidation else 0.0

            cur = Trade(entry_idx=i, entry_price=entry, initial_stop=stop,
                          target_price=target)
            in_pos = True
            bar_idx += 1
        else:
            j = bar_idx
            xp = None
            reason = ""

            # Stop hit?
            if lows[j] <= cur.initial_stop:
                xp = cur.initial_stop
                reason = "stop"
            # Target hit (only if not trailing)?
            elif not trail_invalidation and cur.target_price > 0 and highs[j] >= cur.target_price:
                xp = cur.target_price
                reason = "target"
            # Trail invalidation: close < this bar's prior_week_low
            elif trail_invalidation:
                pwl_j = prior_week_low[j]
                if not np.isnan(pwl_j) and closes[j] < pwl_j:
                    xp = closes[j]
                    reason = "trail_invalidated"

            if xp is not None:
                slip = atr[j] * SLIPPAGE
                xp -= slip if reason != "stop" else 0  # entry slip on stop only
                if reason == "target":
                    xp -= slip  # exit slip on target too (adverse)
                pnl = (xp - cur.entry_price) * lots * mpu - 2 * COMMISSION
                risk_dollars = (cur.entry_price - cur.initial_stop) * lots * mpu
                R = pnl / risk_dollars if risk_dollars > 0 else 0.0
                cur.exit_idx = j
                cur.exit_price = xp
                cur.realized_pnl = pnl
                cur.R_multiple = R
                cur.bars_held = j - cur.entry_idx
                cur.exit_reason = reason
                trades.append(cur)
                in_pos = False
                cur = None
            bar_idx += 1
    return trades


def stats_dict(trades: list[Trade]) -> dict:
    if not trades:
        return dict(n=0, pf=0, wr=0, mean_R=0, gain=0, max_dd_pct=0,
                       avg_hold=0, recovery_days=None,
                       longest_loss_streak=0)
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    gp = sum(t.realized_pnl for t in wins)
    gl = -sum(t.realized_pnl for t in losses)
    pf = gp/gl if gl > 0 else (9.99 if gp > 0 else 0)
    eq = [0.0]
    for t in trades: eq.append(eq[-1] + t.realized_pnl)
    eq_arr = pd.Series(eq); peak = eq_arr.cummax()
    dd_series = peak - eq_arr
    max_dd_idx = int(dd_series.idxmax())
    max_dd = float(dd_series.iloc[max_dd_idx])
    # Recovery
    recovery_days = None
    if max_dd > 1 and max_dd_idx < len(eq) - 1:
        peak_v = peak.iloc[max_dd_idx]
        for k in range(max_dd_idx, len(eq)):
            if eq[k] >= peak_v:
                recovery_days = k - max_dd_idx
                break
    # Longest loss streak
    cur_l = 0; longest = 0
    for t in trades:
        if t.realized_pnl < 0:
            cur_l += 1; longest = max(longest, cur_l)
        else: cur_l = 0
    return dict(
        n=len(trades), pf=round(pf, 3),
        wr=round(len(wins)/len(trades)*100, 1),
        mean_R=round(sum(t.R_multiple for t in trades)/len(trades), 3),
        gain=round(sum(t.realized_pnl for t in trades), 0),
        max_dd_pct=round(max_dd / 100_000 * 100, 2),
        avg_hold=round(sum(t.bars_held for t in trades)/len(trades), 1),
        recovery_days=recovery_days,
        longest_loss_streak=longest,
    )


VARIANTS = [
    ("RR_1_1",   dict(target_R=1.0, trail_invalidation=False)),
    ("RR_1_2",   dict(target_R=2.0, trail_invalidation=False)),
    ("RR_1_3",   dict(target_R=3.0, trail_invalidation=False)),
    ("RR_1_4",   dict(target_R=4.0, trail_invalidation=False)),
    ("RR_trail", dict(target_R=0.0, trail_invalidation=True)),
]


def main():
    log = experiment_log.load()
    print(f"Loaded log: {len(log.records)} prior records")

    new = 0
    for ticker, mpu, lots in TICKERS:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists(): continue
        df = load_parquet(ppath)
        if len(df) < 300: continue
        for vname, params in VARIANTS:
            if log.has(strategy="last_week_low", variant=vname,
                          ticker=ticker, tf="D1"):
                continue
            try:
                trades = run_last_week_low(
                    df, lots=lots, mpu=mpu,
                    use_50_200_filter=True, **params)
                s = stats_dict(trades)
                log.record(strategy="last_week_low", variant=vname,
                              ticker=ticker, tf="D1",
                              stats=s, params=params)
                new += 1
            except Exception as e:
                print(f"err {ticker} {vname}: {e}")
    log.save()
    print(f"New runs: {new}")
    print()

    # Print results matrix
    print("=" * 130)
    print("LAST-WEEK-LOW REVERSAL — R:R variation matrix (D1, 50EMA>200EMA filter)")
    print("=" * 130)
    print(f"{'Ticker':12} | "
          + " | ".join(f"{v[3:]:>14}" for v, _ in VARIANTS))
    print("-" * 12 + "-+-" + "-+-".join("-" * 14 for _ in VARIANTS))

    safe_cells = []
    for ticker, _, _ in TICKERS:
        cells = []
        for vname, _ in VARIANTS:
            r = log.get(strategy="last_week_low", variant=vname,
                          ticker=ticker, tf="D1")
            if r is None:
                cells.append("(none)")
                continue
            s = r.stats
            recovered = (s.get("recovery_days") is not None
                          or float(s.get("max_dd_pct", 100)) < 0.5)
            mark = ("✓" if r.deploy_safe and recovered else "✗")
            cell = (f"{mark} n{int(s.get('n',0)):>3} "
                    f"PF{float(s.get('pf',0)):.2f} "
                    f"R{float(s.get('mean_R',0)):+.2f}")
            cells.append(cell)
            if r.deploy_safe and recovered:
                safe_cells.append((vname, ticker, r))
        print(f"{ticker[:12]:12} | "
              + " | ".join(f"{c[:14]:>14}" for c in cells))

    # Best cells (deploy-safe AND recovered)
    print()
    if safe_cells:
        print(f"DEPLOY-SAFE CELLS THAT RECOVERED ({len(safe_cells)}):")
        print(f"  {'Variant':10} {'Ticker':12} {'n':>4} {'PF':>5} {'WR%':>4} "
              f"{'mean_R':>7} {'gain$':>10} {'DD%':>5} {'rec':>6} "
              f"{'longestL':>9}")
        safe_cells.sort(key=lambda x: -float(x[2].stats.get("gain", 0)))
        for vname, ticker, r in safe_cells:
            s = r.stats
            rec = s.get("recovery_days")
            rec_s = f"{rec}d" if rec is not None else "0d"
            print(f"  {vname:10} {ticker[:12]:12} "
                  f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
                  f"{float(s.get('wr',0)):>3.0f}% "
                  f"{float(s.get('mean_R',0)):>+7.2f} "
                  f"${float(s.get('gain',0)):>+9,.0f} "
                  f"{float(s.get('max_dd_pct',0)):>4.1f}% "
                  f"{rec_s:>6} "
                  f"{int(s.get('longest_loss_streak',0)):>9}")
    else:
        print("No deploy-safe cells found.")


if __name__ == "__main__":
    main()
