#!/usr/bin/env python3
"""
sweep_ema_spread_pullback.py — trend pullback gated by EMA-fan separation.

Avoid the chop: only trade when EMA9 / EMA21 / EMA50 are clearly
separated and ordered. The "spread" is (EMA9 − EMA50) / close. When
that's small, the EMAs are clustered and price is going sideways —
we sit out. When it's large, there's a genuine trend to ride.

Setup (long-only):
  1. EMA9 > EMA21 > EMA50  (clean uptrend)
  2. (EMA9 − EMA50) / close > 0.5%  (fanned, not clustered)
  3. Pullback: bar low touches within 0.3% of EMA21
  4. Bounce: close > open AND close > EMA9
  Stop = bar low − 0.4 × ATR

Exits (variations):
  R:R 1:1, 1:2, 1:3 fixed
  trail = ride until close < EMA21 (trend break)

Universe: metals (4) + indices (7) on D1 + H1
Saves to data/experiment_log.json under strategy="ema_spread_pullback".
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

PULLBACK_TOUCH_PCT = 0.003   # within 0.3% of EMA21
SPREAD_THRESHOLD = 0.005     # (ema9 − ema50)/close > 0.5%
STOP_ATR_FRAC = 0.4


def run_strategy(df: pd.DataFrame, *, lots: float, mpu: float,
                 rr_target: Optional[float] = 2.0) -> list[dict]:
    n = len(df)
    if n < 200:
        return []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    atr = atr_wilder(df, 14).values
    e9 = ema(df["close"], 9).values
    e21 = ema(df["close"], 21).values
    e50 = ema(df["close"], 50).values
    spread = (e9 - e50) / np.where(closes > 0, closes, 1.0)

    trades: list[dict] = []
    in_pos = False
    cur: Optional[dict] = None

    for i in range(60, n):
        if not in_pos:
            if np.isnan(e50[i]) or np.isnan(atr[i]):
                continue
            # Clean uptrend with fanned EMAs
            if not (e9[i] > e21[i] > e50[i]):
                continue
            if spread[i] < SPREAD_THRESHOLD:
                continue
            # Pullback to EMA21
            touched = abs(lows[i] - e21[i]) / e21[i] < PULLBACK_TOUCH_PCT \
                or (lows[i] <= e21[i] <= highs[i])
            bounced = closes[i] > opens[i] and closes[i] > e9[i]
            if touched and bounced:
                slip = atr[i] * SLIPPAGE
                entry = closes[i] + slip
                stop = lows[i] - STOP_ATR_FRAC * atr[i]
                if stop > 0 and stop < entry:
                    risk_per_unit = entry - stop
                    target = entry + rr_target * risk_per_unit \
                        if rr_target is not None else None
                    cur = {"dir": +1, "entry_idx": i, "entry": entry,
                           "stop": stop, "target": target,
                           "risk_per_unit": risk_per_unit}
                    in_pos = True
        else:
            j = i
            xp = None
            if lows[j] <= cur["stop"]:
                xp = cur["stop"]
            elif cur["target"] is not None and highs[j] >= cur["target"]:
                xp = cur["target"]
            elif cur["target"] is None and closes[j] < e21[j]:
                xp = closes[j]
            if xp is not None:
                if xp != cur["target"]:
                    xp -= atr[j] * SLIPPAGE
                pnl = (xp - cur["entry"]) * lots * mpu - 2 * COMMISSION
                risk = cur["risk_per_unit"] * lots * mpu
                R = pnl / risk if risk > 0 else 0
                cur.update(exit_idx=j, pnl=pnl, R=R,
                           bars=j - cur["entry_idx"])
                trades.append(cur)
                in_pos = False
                cur = None
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
    ("RR_1_1",   dict(rr_target=1.0)),
    ("RR_1_2",   dict(rr_target=2.0)),
    ("RR_1_3",   dict(rr_target=3.0)),
    ("RR_trail", dict(rr_target=None)),
]


def main():
    log = experiment_log.load()
    print(f"Loaded log: {len(log.records)} prior records")

    new = 0
    for ticker, mpu, lots in TICKERS:
        for tf in ("D1", "H1"):
            ppath = ROOT / "data" / f"{ticker}_{tf}.parquet"
            if not ppath.exists():
                continue
            df = load_parquet(ppath)
            if len(df) < 200:
                continue
            for vname, params in VARIANTS:
                if log.has(strategy="ema_spread_pullback", variant=vname,
                           ticker=ticker, tf=tf):
                    continue
                try:
                    trades = run_strategy(df, lots=lots, mpu=mpu, **params)
                    s = stats_dict(trades)
                    log.record(
                        strategy="ema_spread_pullback", variant=vname,
                        ticker=ticker, tf=tf,
                        stats=s, params=params,
                    )
                    new += 1
                except Exception as e:
                    print(f"err {ticker} {tf} {vname}: {e}")
    log.save()
    print(f"New runs: {new}")
    print()

    es_records = [r for r in log.records.values()
                  if r.strategy == "ema_spread_pullback"]
    safe = [r for r in es_records
            if r.deploy_safe and float(r.stats.get('pf', 0)) > 1.3]
    safe.sort(key=lambda r: -float(r.stats.get("pf", 0))
              * max(0.001, float(r.stats.get("mean_R", 0))))
    print("=" * 110)
    print(f"DEPLOY-SAFE EMA-SPREAD PULLBACK CELLS PF>1.3 ({len(safe)}):")
    print("=" * 110)
    print(f"  {'Variant':10} {'Ticker':12} {'TF':4} {'n':>4} {'PF':>5} "
          f"{'WR%':>4} {'mean_R':>7} {'gain$':>10} {'DD%':>5}")
    for r in safe[:25]:
        s = r.stats
        print(f"  {r.variant[:10]:10} {r.ticker[:12]:12} {r.tf:4} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('wr',0)):>3.0f}% "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"${float(s.get('gain',0)):>+9,.0f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}%")

    # Combined leaderboard across ALL strategies in log
    print()
    print("=" * 110)
    print("MASTER LEADERBOARD — top 25 deploy-safe cells across every strategy")
    print("=" * 110)
    all_safe = [r for r in log.records.values() if r.deploy_safe]
    all_safe.sort(
        key=lambda r: -(float(r.stats.get("pf", 0))
                        * max(0.001, float(r.stats.get("mean_R", 0)))))
    print(f"  {'Strategy':22} {'Variant':10} {'Ticker':12} {'TF':4} "
          f"{'n':>4} {'PF':>5} {'mean_R':>7} {'gain$':>10} {'DD%':>5}")
    for r in all_safe[:25]:
        s = r.stats
        print(f"  {r.strategy[:22]:22} {r.variant[:10]:10} "
              f"{r.ticker[:12]:12} {r.tf:4} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"${float(s.get('gain',0)):>+9,.0f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}%")


if __name__ == "__main__":
    main()
