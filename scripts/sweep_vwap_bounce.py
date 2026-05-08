#!/usr/bin/env python3
"""
sweep_vwap_bounce.py — VWAP bounce strategy on H1 with daily anchor.

Mechanism:
  Anchor VWAP at each new UTC day's start (00:00).
  Cumulate (typical_price * volume) and volume from anchor.
  VWAP_t = sum(TP*V)[anchor..t] / sum(V)[anchor..t]

  Setup:
    1. Day's bias = bullish if H1 close > VWAP for 3+ bars after anchor settles
    2. Entry (long): bar low touches within 0.15% of VWAP (a pullback to VWAP support)
                     AND close > open (rejection / bounce candle)
    3. (Optional) Daily trend filter: D1 50-EMA > 200-EMA
    Stop = VWAP - 0.5 × ATR(14)  (just below VWAP touch)

  Exit variations:
    R:R 1:1, 1:2, 1:3 fixed
    "trail" = ride until H1 close < VWAP

Universe: metals (4) + indices (7) on H1
Saves to data/experiment_log.json under strategy="vwap_bounce".
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

VWAP_TOUCH_PCT = 0.0015     # within 0.15% of VWAP
VWAP_STOP_ATR = 0.5
MIN_BARS_AFTER_ANCHOR = 3   # let VWAP settle before signaling


def compute_session_vwap(df: pd.DataFrame) -> np.ndarray:
    """Anchor VWAP at each UTC day boundary."""
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    if "tick_volume" in df.columns:
        vol = df["tick_volume"].values.astype(float)
    elif "volume" in df.columns:
        vol = df["volume"].values.astype(float)
    else:
        # synthetic volume = (high - low) — proxies activity
        vol = (df["high"] - df["low"]).values.astype(float)
    vol = np.where(vol > 0, vol, 1.0)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    day_id = df["time"].dt.normalize().values  # day boundary

    vwap = np.empty(len(df))
    bars_in_session = np.zeros(len(df), dtype=int)
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
        cum_pv += tp[i] * vol[i]
        cum_v += vol[i]
        bar_in_session += 1
        bars_in_session[i] = bar_in_session
        vwap[i] = cum_pv / cum_v if cum_v > 0 else tp[i]
    return vwap, bars_in_session


def run_vwap_bounce(df: pd.DataFrame, *, lots: float, mpu: float,
                    rr_target: Optional[float] = 2.0,  # None = trail
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
    vwap, bars_in_sess = compute_session_vwap(df)

    # D1 filter: align by date
    bullish_regime = np.ones(n, dtype=bool)
    if use_d1_filter and df_d1 is not None and len(df_d1) > 200:
        d1_50 = ema(df_d1["close"], 50).values
        d1_200 = ema(df_d1["close"], 200).values
        d1_time = pd.to_datetime(df_d1["time"]).values
        h1_time = pd.to_datetime(df["time"]).values
        # for each H1 bar, find prior D1 bar's regime
        d1_dates = pd.to_datetime(df_d1["time"]).dt.normalize().values
        h1_dates = pd.to_datetime(df["time"]).dt.normalize().values
        d1_bull = (d1_50 > d1_200)
        d1_idx_map = {pd.Timestamp(d): i for i, d in enumerate(d1_dates)}
        for i in range(n):
            d = pd.Timestamp(h1_dates[i])
            # use yesterday's D1 close to avoid look-ahead
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

    for i in range(50, n):
        if not in_pos:
            if bars_in_sess[i] < MIN_BARS_AFTER_ANCHOR:
                continue
            if not bullish_regime[i]:
                continue
            if np.isnan(vwap[i]) or np.isnan(atr[i]):
                continue
            # Pullback test: bar low touches within 0.15% of VWAP
            touch = abs(lows[i] - vwap[i]) / vwap[i] < VWAP_TOUCH_PCT \
                or (lows[i] <= vwap[i] <= highs[i])
            bounce = closes[i] > opens[i] and closes[i] > vwap[i]
            if touch and bounce:
                slip = atr[i] * SLIPPAGE
                entry = closes[i] + slip
                stop = vwap[i] - VWAP_STOP_ATR * atr[i]
                if stop > 0 and stop < entry:
                    risk_per_unit = entry - stop
                    if rr_target is not None:
                        target = entry + rr_target * risk_per_unit
                    else:
                        target = None
                    cur = {"dir": +1, "entry_idx": i, "entry": entry,
                           "stop": stop, "target": target,
                           "risk_per_unit": risk_per_unit}
                    in_pos = True
        else:
            j = i
            xp = None
            # Stop hit
            if lows[j] <= cur["stop"]:
                xp = cur["stop"]
            # Target hit
            elif cur["target"] is not None and highs[j] >= cur["target"]:
                xp = cur["target"]
            # Trail exit: close < VWAP
            elif cur["target"] is None:
                if closes[j] < vwap[j]:
                    xp = closes[j]
            if xp is not None:
                xp -= atr[j] * SLIPPAGE if xp != cur["target"] else 0
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
    ("RR_1_1",   dict(rr_target=1.0, use_d1_filter=True)),
    ("RR_1_2",   dict(rr_target=2.0, use_d1_filter=True)),
    ("RR_1_3",   dict(rr_target=3.0, use_d1_filter=True)),
    ("RR_trail", dict(rr_target=None, use_d1_filter=True)),
    ("RR_1_2_nofilt", dict(rr_target=2.0, use_d1_filter=False)),
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
            if log.has(strategy="vwap_bounce", variant=vname,
                       ticker=ticker, tf="H1"):
                continue
            try:
                trades = run_vwap_bounce(df_h1, lots=lots, mpu=mpu,
                                         df_d1=df_d1, **params)
                s = stats_dict(trades)
                log.record(
                    strategy="vwap_bounce", variant=vname,
                    ticker=ticker, tf="H1",
                    stats=s, params=params,
                )
                new += 1
            except Exception as e:
                print(f"err {ticker} {vname}: {e}")
    log.save()
    print(f"New runs: {new}")
    print()

    # Matrix
    vw_records = [r for r in log.records.values()
                  if r.strategy == "vwap_bounce"]
    cells = {(r.ticker, r.variant): r for r in vw_records}
    print("=" * 110)
    print("VWAP BOUNCE — R:R variation matrix (H1, D1 50/200 filter where shown)")
    print("=" * 110)
    header = f"{'Ticker':12} | "
    for vn, _ in VARIANTS:
        header += f"{vn:>14} | "
    print(header)
    print("-" * len(header))
    for ticker, _, _ in TICKERS:
        row = f"{ticker[:12]:12} | "
        for vname, _ in VARIANTS:
            r = cells.get((ticker, vname))
            if not r:
                row += f"{'—':>14} | "
            else:
                tag = "✓" if r.deploy_safe else "✗"
                row += (f"{tag} n {int(r.stats.get('n',0)):>3} "
                        f"PF{float(r.stats.get('pf',0)):.2f}".ljust(14)
                        + " | ")
        print(row)

    # Deploy-safe with PF>1.3
    safe = [r for r in vw_records
            if r.deploy_safe and float(r.stats.get('pf', 0)) > 1.3]
    safe.sort(key=lambda r: -float(r.stats.get("pf", 0))
              * max(0.001, float(r.stats.get("mean_R", 0))))
    print()
    print(f"DEPLOY-SAFE VWAP CELLS WITH PF>1.3 ({len(safe)}):")
    print(f"  {'Variant':16} {'Ticker':12} {'n':>4} {'PF':>5} "
          f"{'WR%':>4} {'mean_R':>7} {'gain$':>10} {'DD%':>5}")
    for r in safe[:20]:
        s = r.stats
        print(f"  {r.variant[:16]:16} {r.ticker[:12]:12} "
              f"{int(s.get('n',0)):>4} {float(s.get('pf',0)):>5.2f} "
              f"{float(s.get('wr',0)):>3.0f}% "
              f"{float(s.get('mean_R',0)):>+7.2f} "
              f"${float(s.get('gain',0)):>+9,.0f} "
              f"{float(s.get('max_dd_pct',0)):>4.1f}%")


if __name__ == "__main__":
    main()
