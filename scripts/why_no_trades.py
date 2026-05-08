#!/usr/bin/env python3
"""why_no_trades.py — comprehensive diagnostic for "runner alive but no
trades" scenarios. Shows for each cell:
  - When it was last evaluated
  - Current indicator state (RSI / EMA cross / Donchian breakout)
  - How far from a setup
  - Whether parquet bars are fresh
  - What the bridge has been called for in the last 30 min
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
from core.data import load_parquet
from core.indicators import rsi_wilder, ema


DB = ROOT / "data" / "accounts" / "531019095" / "v2.db"


def main():
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    print(f"NOW (UTC): {now.isoformat()[:19]}    {now.strftime('%A')}\n")

    # 1. Bridge calls last 30 min
    print("=" * 80)
    print("BRIDGE EVENTS LAST 30 MIN")
    print("=" * 80)
    since = (now - timedelta(minutes=30)).isoformat()
    rows = con.execute(
        "SELECT method, COUNT(*) n FROM bridge_events "
        "WHERE pinged_at_utc >= ? GROUP BY method ORDER BY n DESC",
        (since,)).fetchall()
    if not rows:
        print("  ⚠️ NO bridge events in 30 min — runner stalled?")
    else:
        for r in rows:
            print(f"  {r['method']:35} {r['n']}")

    # 2. Deployments + last-evaluated
    print("\n" + "=" * 80)
    print("DEPLOYMENTS")
    print("=" * 80)
    with open(ROOT / "data/accounts/531019095/deployments.json") as f:
        deps = json.load(f)
    print(f"Total: {len(deps)}\n")
    for d in deps:
        le = d.get("last_evaluated_at_utc")
        age = "—"
        if le:
            try:
                t = datetime.fromisoformat(le.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                mins = (now - t).total_seconds() / 60
                age = f"{abs(mins):.0f}m" + (" old" if mins >= 0 else " (tz)")
            except Exception:
                pass
        flag = "🟢" if d.get("status") == "live" else "📄"
        print(f"  {flag} {d.get('status'):6} {d.get('deployment_id', '')[:42]:42}  eval={age}")

    # 3. Indicator state per cell
    print("\n" + "=" * 80)
    print("INDICATOR STATE — how close to firing")
    print("=" * 80)
    for d in deps:
        sname = d.get("strategy", "")
        ticker = d.get("ticker", "")
        tf = d.get("tf", "")
        p = ROOT / "data" / f"{ticker}_{tf}.parquet"
        if not p.exists():
            continue
        try:
            df = load_parquet(p)
            last_close = df["close"].iloc[-1]
            closes = df["close"]
            label = f"{ticker} {tf} {sname}"[:50]
            if "rsi" in sname:
                r = rsi_wilder(closes, 14).iloc[-1]
                if r < 30:
                    state = f"🟢 BUY ZONE (RSI={r:.1f})"
                elif r > 70:
                    state = f"🔴 SELL ZONE (RSI={r:.1f})"
                else:
                    nearest = min(r - 30, 70 - r)
                    state = f"⚪ wait RSI={r:.1f}  {nearest:.1f}pts to extreme"
            elif "ema_cross" in sname:
                if "9_20" in sname:
                    f_v = ema(closes, 9).iloc[-1]
                    s_v = ema(closes, 20).iloc[-1]
                    fp = ema(closes, 9).iloc[-2]
                    sp = ema(closes, 20).iloc[-2]
                elif "12_26" in sname:
                    f_v = ema(closes, 12).iloc[-1]
                    s_v = ema(closes, 26).iloc[-1]
                    fp = ema(closes, 12).iloc[-2]
                    sp = ema(closes, 26).iloc[-2]
                else:
                    f_v = s_v = fp = sp = 0
                cross = (f_v > s_v) != (fp > sp)
                spread = (f_v - s_v) / last_close * 100
                if cross:
                    state = (f"🟢 CROSSED " + ("UP" if f_v > s_v else "DOWN"))
                else:
                    state = (f"⚪ {'BULL' if f_v > s_v else 'BEAR'} stable, "
                             f"spread={spread:+.2f}%")
            elif "donchian" in sname:
                period = 20 if "20" in sname else 55
                prior_hi = df["high"].iloc[-period - 1:-1].max()
                prior_lo = df["low"].iloc[-period - 1:-1].min()
                if last_close > prior_hi:
                    state = f"🟢 BREAKOUT above {period}-bar high"
                elif last_close < prior_lo:
                    state = f"🔴 BREAKDOWN below {period}-bar low"
                else:
                    gap_h = (prior_hi - last_close) / last_close * 100
                    gap_l = (last_close - prior_lo) / last_close * 100
                    state = (f"⚪ +{gap_h:.2f}% to {period}-high / "
                             f"-{gap_l:.2f}% to {period}-low")
            else:
                state = "(unrecognized strategy)"
            print(f"  {label:50} | {state}")
        except Exception as e:
            print(f"  {label:50} | ERROR: {e}")

    # 4. Parquet freshness
    print("\n" + "=" * 80)
    print("PARQUET FRESHNESS (latest bar age)")
    print("=" * 80)
    seen = set()
    for d in deps:
        key = (d["ticker"], d["tf"])
        if key in seen:
            continue
        seen.add(key)
        p = ROOT / "data" / f"{d['ticker']}_{d['tf']}.parquet"
        if not p.exists():
            continue
        df = load_parquet(p)
        last_bar = pd.to_datetime(df["time"].iloc[-1])
        if last_bar.tzinfo is None:
            last_bar = last_bar.tz_localize("UTC")
        age_min = (now - last_bar).total_seconds() / 60
        # M15 freshness threshold = 16 min, H1 = 65 min, D1 = 1500 min
        thresh = {"M15": 20, "H1": 70, "D1": 1500}.get(d["tf"], 60)
        flag = ("✅" if age_min < thresh
                else ("⚠" if age_min < thresh * 2 else "🚫"))
        print(f"  {flag} {d['ticker']:14} {d['tf']:4} last bar "
              f"{age_min:>6.0f}m ago  (thresh {thresh}m)")

    # 5. Trades fired — any?
    print("\n" + "=" * 80)
    print("TRADES OPENED LAST 24H")
    print("=" * 80)
    since24 = (now - timedelta(hours=24)).isoformat()
    rows = con.execute(
        "SELECT mode, strategy, symbol, opened_at_utc, "
        "realized_pnl FROM trades WHERE opened_at_utc >= ? "
        "ORDER BY opened_at_utc DESC", (since24,)).fetchall()
    if not rows:
        print("  No trades in last 24h.")
    for r in rows:
        print(f"  {r['mode']:7} {r['strategy']:24} {r['symbol']:12} "
              f"{r['opened_at_utc'][:19]}  pnl=${r['realized_pnl'] or 0:+.2f}")
    con.close()


if __name__ == "__main__":
    main()
