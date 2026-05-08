#!/usr/bin/env python3
"""H1 variant sweep — same ema9_trail logic on H1 bars.
H1 data is limited (~1.4y), so trade-count is the key metric:
need n>=50 to be statistically meaningful."""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from core import cost_defaults
from core.data import load_parquet
from core.indicators import ema, atr_wilder, rsi_wilder

TICKERS = [
    ("XAUUSD",     100.0,    0.30),
    ("XAGUSD",     5_000.0,  0.40),
    ("XPDUSD",     100.0,    0.20),
    ("GER40.cash", 1.0,      3.0),
    ("US100.cash", 1.0,      6.5),
    ("US500.cash", 1.0,      5.0),
]
COMMISSION = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC

def run(df, *, lots, mpu, use_50ema=False, trail_lookback=1, use_rsi=False):
    n = len(df)
    if n < 60: return []
    closes, highs, lows, opens = df["close"].values, df["high"].values, df["low"].values, df["open"].values
    ema9 = ema(df["close"], 9).values
    ema50 = ema(df["close"], 50).values
    rsi = rsi_wilder(df["close"], 14).values
    atr = atr_wilder(df, 14).values
    trades, in_pos, cur, i = [], False, None, 60
    while i < n:
        if not in_pos:
            ok = all(closes[i-k] > ema9[i-k] for k in (1,2,3) if not np.isnan(ema9[i-k]))
            if use_50ema: ok = ok and all(closes[i-k] > ema50[i-k] for k in (1,2,3) if not np.isnan(ema50[i-k]))
            if use_rsi: ok = ok and all(rsi[i-k] > 55 for k in (1,2,3) if not np.isnan(rsi[i-k]))
            if not ok: i += 1; continue
            three_high = max(highs[i-1], highs[i-2], highs[i-3])
            if closes[i] <= three_high: i += 1; continue
            three_low = min(lows[i-1], lows[i-2], lows[i-3])
            stop = three_low * 0.999
            slip = atr[i] * SLIPPAGE
            cur = {"entry_idx": i, "entry": closes[i] + slip, "stop": stop}
            in_pos = True
            i += 1
        else:
            tl = max(0, i - trail_lookback)
            trail = min(lows[tl:i]) if i > tl else lows[i-1]
            xp = None
            if lows[i] <= cur["stop"]: xp = cur["stop"]
            elif opens[i] < trail: xp = opens[i]
            elif closes[i] < trail: xp = closes[i]
            if xp is not None:
                xp -= atr[i] * SLIPPAGE
                pnl = (xp - cur["entry"]) * lots * mpu - 2 * COMMISSION
                risk = (cur["entry"] - cur["stop"]) * lots * mpu
                R = pnl / risk if risk > 0 else 0
                cur["exit_idx"] = i; cur["pnl"] = pnl; cur["R"] = R
                cur["bars"] = i - cur["entry_idx"]
                trades.append(cur); in_pos = False; cur = None
            i += 1
    return trades

def stats(trades):
    if not trades: return {}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gp = sum(t["pnl"] for t in wins); gl = -sum(t["pnl"] for t in losses)
    pf = gp/gl if gl > 0 else (9.99 if gp > 0 else 0)
    eq = [0]
    for t in trades: eq.append(eq[-1] + t["pnl"])
    eq_arr = pd.Series(eq); peak = eq_arr.cummax()
    dd = float((peak - eq_arr).max())
    return {
        "n": len(trades), "pf": pf, "wr": len(wins)/len(trades)*100,
        "mean_R": sum(t["R"] for t in trades)/len(trades),
        "gain": sum(t["pnl"] for t in trades),
        "max_dd_pct": dd / 100_000 * 100,
        "avg_hold_bars": sum(t["bars"] for t in trades)/len(trades),
    }

VARIANTS = [
    ("V1", dict(use_50ema=False, trail_lookback=1, use_rsi=False)),
    ("V2", dict(use_50ema=True, trail_lookback=1, use_rsi=False)),
    ("V3", dict(use_50ema=False, trail_lookback=2, use_rsi=False)),
    ("V4", dict(use_50ema=True, trail_lookback=2, use_rsi=False)),
    ("V7", dict(use_50ema=True, trail_lookback=2, use_rsi=True)),
]

print(f"\nH1 sweep — ema9_trail variants (~1.4y of H1 data)")
print(f"Note: H1 has 24× more bars than D1 → expect more trades but shorter history")
print()
print(f"{'Ticker':12} {'V1':>22} {'V2':>22} {'V3':>22} {'V4':>22} {'V7':>22}")
print("-" * 122)
safe_cells = []
for tic, mpu, lots in TICKERS:
    p = Path(f"data/{tic}_H1.parquet")
    if not p.exists(): continue
    df = load_parquet(p)
    cells = []
    for vname, params in VARIANTS:
        try:
            t = run(df, lots=lots, mpu=mpu, **params)
            s = stats(t)
            if not s:
                cells.append("(no trades)"); continue
            mark = "✓" if (s["n"]>=15 and s["pf"]>=1.05 and s["mean_R"]>0) else "✗"
            cell = f"{mark}n{s['n']:>3} PF{s['pf']:>4.2f} R{s['mean_R']:>+.2f}"
            cells.append(cell)
            if mark == "✓":
                safe_cells.append((vname, tic, s))
        except Exception as e:
            cells.append(f"err: {str(e)[:15]}")
    print(f"{tic[:12]:12} " + "  ".join(f"{c:>22}" for c in cells))

if safe_cells:
    print()
    print(f"DEPLOY-SAFE H1 CELLS ({len(safe_cells)}):")
    safe_cells.sort(key=lambda x: -x[2]["pf"] * x[2]["mean_R"])
    print(f"  {'Variant':6} {'Ticker':12} {'n':>4} {'PF':>5} {'WR%':>4} {'mean_R':>7} "
          f"{'gain$':>10} {'DD%':>5} {'hold_bars':>9}")
    for vname, tic, s in safe_cells:
        print(f"  {vname:6} {tic[:12]:12} {s['n']:>4} {s['pf']:>5.2f} "
              f"{s['wr']:>3.0f}% {s['mean_R']:>+7.2f} "
              f"${s['gain']:>+9,.0f} {s['max_dd_pct']:>4.1f}% "
              f"{s['avg_hold_bars']:>8.1f}")
else:
    print("\nNo deploy-safe H1 cells.")
