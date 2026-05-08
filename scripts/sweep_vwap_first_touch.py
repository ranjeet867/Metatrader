#!/usr/bin/env python3
"""
sweep_vwap_first_touch.py — disciplined VWAP bounce: first-touch-of-day only.

The naive H1 VWAP bounce fired 500-800 trades per cell — too noisy.
This version is far more selective:

  Setup (per UTC day):
    1. Wait for VWAP to settle (5+ bars after anchor)
    2. Track if price has been above VWAP since anchor settled (bullish day)
    3. FIRST time bar low touches VWAP after that:
        - close > open (bounce)
        - RSI(14) on H1 < 50 (oversold-ish, but not extreme)
        - 50EMA > 200EMA on D1 (trend filter)
    Only ONE trade per day.
    Stop = VWAP - 0.7 × ATR  (more breathing room)

  Exit variations:
    R:R 1:1, 1:2, 1:3 fixed
    "trail" = ride until close < VWAP

Universe: metals (4) + indices (7) on H1
Saves to data/experiment_log.json under strategy="vwap_first_touch".
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
from core.indicators import ema, atr_wilder, rsi_wilder as rsi

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

VWAP_TOUCH_PCT = 0.002      # within 0.2% of VWAP
VWAP_STOP_ATR = 0.7
MIN_BARS_AFTER_ANCHOR = 5
RSI_MAX_AT_ENTRY = 50


def compute_session_vwap(df: pd.DataFrame):
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    if "tick_volume" in df.columns:
        vol = df["tick_volume"].values.astype(float)
    elif "volume" in df.columns:
        vol = df["volume"].values.astype(float)
    else:
        vol = (df["high"] - df["low"]).values.astype(float)
    vol = np.where(vol > 0, vol, 1.0)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    day_id = df["time"].dt.normalize().values

    vwap = np.empty(len(df))
    bars_in_session = np.zeros(len(df), dtype=int)
    is_new_day = np.zeros(len(df), dtype=bool)
    cum_pv = 0.0
    cum_v = 0.0
    cur_day = None
    bar_in_session = 0
    for i in range(len(df)):
        if cur_day != day_id[i]:
            cum_pv = 0.0
            cum_v = 0.0
            cur_day = day_id[i]
            bar_in_session = 0
            is_new_day[i] = True
        cum_pv += tp[i] * vol[i]
        cum_v += vol[i]
        bar_in_session += 1
        bars_in_session[i] = bar_in_session
        vwap[i] = cum_pv / cum_v if cum_v > 0 else tp[i]
    return vwap, bars_in_session, is_new_day


def run_strategy(df: pd.DataFrame, *, lots: float, mpu: float,
                 rr_target: Optional[float] = 2.0,
                 use_d1_filter: bool = True,
                 df_d1: Optional[pd.DataFrame] = None) -> list[dict]:
    n = len(df)
    if n < 200:
        return []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    atr = atr_wilder(df, 14).values
    rsi14 = rsi(df["close"], 14).values
    vwap, bars_in_sess, is_new_day = compute_session_vwap(df)

    # D1 filter
    bullish_regime = np.ones(n, dtype=bool)
    if use_d1_filter and df_d1 is not None and len(df_d1) > 200:
        d1_50 = ema(df_d1["close"], 50).values
        d1_200 = ema(df_d1["close"], 200).values
        d1_dates = pd.to_datetime(df_d1["time"]).dt.normalize().values
        h1_dates = pd.to_datetime(df["time"]).dt.normalize().values
        d1_bull = (d1_50 > d1_200)
        d1_idx_map = {pd.Timestamp(d): i for i, d in enumerate(d1_dates)}
        for i in range(n):
            d = pd.Timestamp(h1_dates[i])
            yesterday = d - pd.Timedelta(days=1)
            for back in range(7):
                key = yesterday - pd.Timedelta(days=back)
                if key in d1_idx_map:
                    j = d1_idx_map[key]
                    bullish_regime[i] = bool(d1_bull[j])
                    break

    trades: list[dict] = []
    in_pos = False
    cur: Optional[dict] = None

    # Per-day state
    today_was_above_vwap = False
    today_traded = False

    for i in range(60, n):
        if is_new_day[i]:
            today_was_above_vwap = False
            today_traded = False

        if not in_pos:
            if bars_in_sess[i] < MIN_BARS_AFTER_ANCHOR:
                if closes[i] > vwap[i]:
                    today_was_above_vwap = True
                continue
            if today_traded or not today_was_above_vwap:
                if closes[i] > vwap[i]:
                    today_was_above_vwap = True
                continue
            if not bullish_regime[i]:
                continue
            if np.isnan(vwap[i]) or np.isnan(atr[i]) or np.isnan(rsi14[i]):
                continue
            # First touch of VWAP this day with bullish setup intact
            touched = lows[i] <= vwap[i] * (1 + VWAP_TOUCH_PCT) \
                and lows[i] >= vwap[i] * (1 - VWAP_TOUCH_PCT)
            bounced = closes[i] > opens[i] and closes[i] > vwap[i]
            rsi_ok = rsi14[i] < RSI_MAX_AT_ENTRY
            if touched and bounced and rsi_ok:
                slip = atr[i] * SLIPPAGE
                entry = closes[i] + slip
                stop = vwap[i] - VWAP_STOP_ATR * atr[i]
                if stop > 0 and stop < entry:
                    risk_per_unit = entry - stop
                    target = entry + rr_target * risk_per_unit \
                        if rr_target is not None else None
                    cur = {"dir": +1, "entry_idx": i, "entry": entry,
                           "stop": stop, "target": target,
                           "risk_per_unit": risk_per_unit}
                    in_pos = True
                    today_traded = True
            if closes[i] > vwap[i]:
                today_was_above_vwap = True
        else:
            j = i
            xp = None
            if lows[j] <= cur["stop"]:
                xp = cur["stop"]
            elif cur["target"] is not None and highs[j] >= cur["target"]:
                xp = cur["target"]
            elif cur["target"] is None and closes[j] < vwap[j]:
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
        h1_path = ROOT / "data" / f"{ticker}_H1.parquet"
        d1_path = ROOT / "data" / f"{ticker}_D1.parquet"
        if not h1_path.exists():
            continue
        df_h1 = load_parquet(h1_path)
        df_d1 = load_parquet(d1_path) if d1_path.exists() else None
        if len(df_h1) < 300:
            continue
        for vname, params in VARIANTS:
            if log.has(strategy="vwap_first_touch", variant=vname,
                       ticker=ticker, tf="H1"):
                continue
            try:
                trades = run_strategy(df_h1, lots=lots, mpu=mpu,
                                      df_d1=df_d1, **params)
                s = stats_dict(trades)
                log.record(
                    strategy="vwap_first_touch", variant=vname,
                    ticker=ticker, tf="H1",
                    stats=s, params=params,
                )
                new += 1
            except Exception as e:
                print(f"err {ticker} {vname}: {e}")
    log.save()
    print(f"New runs: {new}")
    print()

    vw_records = [r for r in log.records.values()
                  if r.strategy == "vwap_first_touch"]
    cells = {(r.ticker, r.variant): r for r in vw_records}
    print("=" * 110)
    print("VWAP FIRST-TOUCH (H1, max 1 trade/day, RSI<50, 50/200 D1 filter)")
    print("=" * 110)
    header = f"{'Ticker':12} | "
    for vn, _ in VARIANTS:
        header += f"{vn:>15} | "
    print(header)
    print("-" * len(header))
    for ticker, _, _ in TICKERS:
        row = f"{ticker[:12]:12} | "
        for vname, _ in VARIANTS:
            r = cells.get((ticker, vname))
            if not r:
                row += f"{'—':>15} | "
            else:
                tag = "✓" if r.deploy_safe else "✗"
                cell = (f"{tag} n {int(r.stats.get('n',0)):>3} "
                        f"PF{float(r.stats.get('pf',0)):.2f}")
                row += f"{cell:15} | "
        print(row)

    safe = [r for r in vw_records if r.deploy_safe
            and float(r.stats.get('pf', 0)) > 1.3]
    safe.sort(key=lambda r: -float(r.stats.get("pf", 0))
              * max(0.001, float(r.stats.get("mean_R", 0))))
    print()
    print(f"DEPLOY-SAFE VWAP-FIRST-TOUCH CELLS PF>1.3 ({len(safe)}):")
    print(f"  {'Variant':12} {'Ticker':12} {'n':>4} {'PF':>5} "
          f"{'WR%':>4} {'mean_R':>7} {'gain$':>10} {'DD%':>5}")
    for r in safe[:20]:
        s = r.stats
        print(f"  {r.variant[:12]:12} {r.ticker[:12]:12} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('wr',0)):>3.0f}% "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"${float(s.get('gain',0)):>+9,.0f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}%")


if __name__ == "__main__":
    main()
