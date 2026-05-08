#!/usr/bin/env python3
"""
comprehensive_report.py — full per-cell deep-dive with year-by-year
P&L, recovery filter, and the V8 + V5_BB extensions.

Adds two new variants to the experiment log:
  - ema9_trail V8: V4 + ATR vol filter (skip dead-vol periods)
  - bb_expansion M15 (V1-V4): squeeze-break on faster TF

Output: per-cell rich stats including year-by-year P&L breakdown.
Skips cells that never recovered from drawdown — those are FTMO-unsafe.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core import cost_defaults, experiment_log
from core.data import load_parquet
from core.indicators import ema, atr_wilder, rsi_wilder

COMMISSION = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC
STARTING = 100_000.0

ALL_TICKERS = [
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


@dataclass
class Trade:
    entry_idx: int = 0
    exit_idx: int = 0
    entry_price: float = 0.0
    exit_price: float = 0.0
    initial_stop: float = 0.0
    realized_pnl: float = 0.0
    R_multiple: float = 0.0
    bars_held: int = 0
    entry_time: pd.Timestamp = None
    exit_time: pd.Timestamp = None
    direction: int = 1


# ─────────────────────────────────────────────────────────────────
# ema9_trail V8: V4 + ATR vol filter
# ─────────────────────────────────────────────────────────────────
def run_ema9_trail_v8(df, *, lots, mpu, atr_filter=True):
    n = len(df)
    if n < 60: return []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    times = pd.to_datetime(df["time"]).values
    ema9 = ema(df["close"], 9).values
    ema50 = ema(df["close"], 50).values
    atr = atr_wilder(df, 14).values
    # Rolling 50-bar median ATR — entry only when current ATR > median
    atr_median = pd.Series(atr).rolling(50).median().values

    trades, in_pos, cur, i = [], False, None, 60
    while i < n:
        if not in_pos:
            ok = all(closes[i-k] > ema9[i-k] for k in (1,2,3) if not np.isnan(ema9[i-k]))
            ok = ok and all(closes[i-k] > ema50[i-k] for k in (1,2,3) if not np.isnan(ema50[i-k]))
            if atr_filter:
                if np.isnan(atr_median[i]) or atr[i] < atr_median[i]:
                    i += 1; continue
            if not ok: i += 1; continue
            three_high = max(highs[i-1], highs[i-2], highs[i-3])
            if closes[i] <= three_high: i += 1; continue
            three_low = min(lows[i-1], lows[i-2], lows[i-3])
            stop = three_low * 0.999
            slip = atr[i] * SLIPPAGE
            cur = Trade(entry_idx=i, entry_price=closes[i] + slip,
                          initial_stop=stop, entry_time=times[i],
                          direction=1)
            in_pos = True
            i += 1
        else:
            tl = max(0, i - 2)  # V4 baseline = 2-bar trail
            trail = min(lows[tl:i]) if i > tl else lows[i-1]
            xp = None
            if lows[i] <= cur.initial_stop: xp = cur.initial_stop
            elif opens[i] < trail: xp = opens[i]
            elif closes[i] < trail: xp = closes[i]
            if xp is not None:
                xp -= atr[i] * SLIPPAGE
                pnl = (xp - cur.entry_price) * lots * mpu - 2 * COMMISSION
                risk = (cur.entry_price - cur.initial_stop) * lots * mpu
                R = pnl / risk if risk > 0 else 0
                cur.exit_idx = i; cur.exit_price = xp
                cur.realized_pnl = pnl; cur.R_multiple = R
                cur.bars_held = i - cur.entry_idx
                cur.exit_time = times[i]
                trades.append(cur)
                in_pos = False; cur = None
            i += 1
    return trades


# ─────────────────────────────────────────────────────────────────
# Rich metrics including year-by-year
# ─────────────────────────────────────────────────────────────────
@dataclass
class RichStats:
    label: str = ""
    n: int = 0
    n_wins: int = 0
    n_losses: int = 0
    starting: float = STARTING
    ending: float = STARTING
    gross_pnl: float = 0.0
    return_pct: float = 0.0
    pf: float = 0.0
    wr: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    rr: float = 0.0
    mean_R: float = 0.0
    max_dd_pct: float = 0.0
    max_dd_usd: float = 0.0
    dd_duration_days: float = 0.0
    recovery_days: Optional[float] = None
    longest_loss_streak: int = 0
    longest_win_streak: int = 0
    avg_hold_days: float = 0.0
    span_years: float = 0.0
    trades_per_year: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    yearly_pnl: dict[int, float] = field(default_factory=dict)


def compute_stats(trades: list[Trade], *, label: str,
                     span_days: float) -> RichStats:
    s = RichStats(label=label)
    if not trades: return s
    s.n = len(trades)
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    s.n_wins = len(wins)
    s.n_losses = len(losses)
    s.gross_pnl = sum(t.realized_pnl for t in trades)
    s.ending = s.starting + s.gross_pnl
    s.return_pct = s.gross_pnl / s.starting * 100
    gp = sum(t.realized_pnl for t in wins)
    gl = -sum(t.realized_pnl for t in losses)
    s.pf = gp/gl if gl > 0 else (9.99 if gp > 0 else 0)
    s.wr = s.n_wins / s.n * 100
    s.avg_win = gp / s.n_wins if s.n_wins else 0
    s.avg_loss = -gl / s.n_losses if s.n_losses else 0
    s.expectancy = s.gross_pnl / s.n
    s.rr = s.avg_win / abs(s.avg_loss) if s.avg_loss else 0
    s.mean_R = sum(t.R_multiple for t in trades) / s.n
    s.avg_hold_days = sum(t.bars_held for t in trades) / s.n
    s.span_years = span_days / 365.25
    s.trades_per_year = s.n / s.span_years if s.span_years > 0 else 0

    # Equity curve
    eq = [s.starting]
    for t in trades: eq.append(eq[-1] + t.realized_pnl)
    eq_arr = pd.Series(eq)
    peak = eq_arr.cummax()
    dd_series = peak - eq_arr
    max_dd_idx = int(dd_series.idxmax())
    s.max_dd_usd = float(dd_series.iloc[max_dd_idx])
    s.max_dd_pct = s.max_dd_usd / s.starting * 100

    pre_peak = int(peak.iloc[:max_dd_idx + 1].idxmax())
    if max_dd_idx > pre_peak and max_dd_idx > 0:
        try:
            t1 = pd.to_datetime(trades[max(0, pre_peak - 1)].exit_time)
            t2 = pd.to_datetime(trades[max(0, max_dd_idx - 1)].exit_time)
            s.dd_duration_days = max(0, (t2-t1).total_seconds()/86400)
        except: s.dd_duration_days = 0.0

    if s.max_dd_usd > 0.01 and max_dd_idx < len(eq) - 1:
        peak_v = peak.iloc[max_dd_idx]
        rec_idx = None
        for k in range(max_dd_idx, len(eq)):
            if eq[k] >= peak_v:
                rec_idx = k; break
        if rec_idx is not None and rec_idx > max_dd_idx:
            try:
                t1 = pd.to_datetime(trades[max(0, max_dd_idx - 1)].exit_time)
                t2 = pd.to_datetime(trades[min(rec_idx - 1, len(trades) - 1)].exit_time)
                s.recovery_days = max(0, (t2-t1).total_seconds()/86400)
            except: s.recovery_days = float(rec_idx - max_dd_idx)

    # Streaks
    cw = cl = 0
    for t in trades:
        if t.realized_pnl > 0:
            cw += 1; s.longest_win_streak = max(s.longest_win_streak, cw); cl = 0
        elif t.realized_pnl < 0:
            cl += 1; s.longest_loss_streak = max(s.longest_loss_streak, cl); cw = 0

    # Sharpe / Sortino / Calmar
    R_series = [t.R_multiple for t in trades]
    if len(R_series) > 1:
        import math
        mu = float(np.mean(R_series))
        std = float(np.std(R_series, ddof=1))
        if std > 0:
            s.sharpe = mu / std * math.sqrt(max(s.trades_per_year, 1))
        neg = [r for r in R_series if r < 0]
        if len(neg) > 1:
            d_std = float(np.std(neg, ddof=1))
            if d_std > 0:
                s.sortino = mu / d_std * math.sqrt(max(s.trades_per_year, 1))
    if s.max_dd_pct > 0 and s.span_years > 0:
        s.calmar = (s.return_pct / s.span_years) / s.max_dd_pct

    # Year-by-year P&L
    yearly = defaultdict(float)
    for t in trades:
        try:
            yr = pd.to_datetime(t.exit_time).year
            yearly[yr] += t.realized_pnl
        except: pass
    s.yearly_pnl = dict(yearly)
    return s


# ─────────────────────────────────────────────────────────────────
# Top cells to deep-dive (the ones we already know are deploy-safe)
# ─────────────────────────────────────────────────────────────────
DEEP_DIVE = [
    # (variant_label, ticker, mpu, lots, run_fn, kwargs)
    ("ema9_trail V7 (RSI55)",  "XAGUSD", 5_000.0, 0.40, "v7"),
    ("ema9_trail V4 (50+2bar)", "XAUUSD", 100.0,   0.30, "v4"),
    ("ema9_trail V4 (50+2bar)", "XAGUSD", 5_000.0, 0.40, "v4"),
    ("ema9_trail V4 (50+2bar)", "GER40.cash", 1.0, 3.0, "v4"),
    ("ema9_trail V4 (50+2bar)", "XPDUSD", 100.0,   0.20, "v4"),
    ("ema9_trail V8 (V4+ATR)", "XAUUSD", 100.0,   0.30, "v8"),
    ("ema9_trail V8 (V4+ATR)", "XAGUSD", 5_000.0, 0.40, "v8"),
    ("ema9_trail V8 (V4+ATR)", "GER40.cash", 1.0, 3.0, "v8"),
    ("bb_expansion V1",        "JP225.cash", 1.0, 2.0, "bb_v1"),
    ("bb_expansion V2 (50EMA)", "XPDUSD", 100.0,   0.20, "bb_v2"),
    ("bb_expansion V1",        "XAUUSD", 100.0,   0.30, "bb_v1"),
]


def run_v4(df, lots, mpu): return _run_ema9(df, lots, mpu, use_50=True, lookback=2, rsi=False, atr_filt=False)
def run_v7(df, lots, mpu): return _run_ema9(df, lots, mpu, use_50=True, lookback=2, rsi=True,  atr_filt=False)
def run_v8(df, lots, mpu): return _run_ema9(df, lots, mpu, use_50=True, lookback=2, rsi=False, atr_filt=True)


def _run_ema9(df, lots, mpu, use_50, lookback, rsi, atr_filt):
    n = len(df)
    if n < 60: return []
    c = df["close"].values; h = df["high"].values; l = df["low"].values; o = df["open"].values
    t = pd.to_datetime(df["time"]).values
    e9 = ema(df["close"], 9).values
    e50 = ema(df["close"], 50).values
    rs = rsi_wilder(df["close"], 14).values
    a = atr_wilder(df, 14).values
    am = pd.Series(a).rolling(50).median().values
    trades, in_pos, cur, i = [], False, None, 60
    while i < n:
        if not in_pos:
            ok = all(c[i-k] > e9[i-k] for k in (1,2,3) if not np.isnan(e9[i-k]))
            if use_50: ok = ok and all(c[i-k] > e50[i-k] for k in (1,2,3) if not np.isnan(e50[i-k]))
            if rsi: ok = ok and all(rs[i-k] > 55 for k in (1,2,3) if not np.isnan(rs[i-k]))
            if atr_filt and (np.isnan(am[i]) or a[i] < am[i]): ok = False
            if not ok: i += 1; continue
            three_h = max(h[i-1], h[i-2], h[i-3])
            if c[i] <= three_h: i += 1; continue
            three_l = min(l[i-1], l[i-2], l[i-3])
            stop = three_l * 0.999
            slip = a[i] * SLIPPAGE
            cur = Trade(entry_idx=i, entry_price=c[i]+slip, initial_stop=stop,
                          entry_time=t[i], direction=1)
            in_pos = True; i += 1
        else:
            tl = max(0, i - lookback)
            trail = min(l[tl:i]) if i > tl else l[i-1]
            xp = None
            if l[i] <= cur.initial_stop: xp = cur.initial_stop
            elif o[i] < trail: xp = o[i]
            elif c[i] < trail: xp = c[i]
            if xp is not None:
                xp -= a[i] * SLIPPAGE
                pnl = (xp - cur.entry_price) * lots * mpu - 2 * COMMISSION
                risk = (cur.entry_price - cur.initial_stop) * lots * mpu
                R = pnl / risk if risk > 0 else 0
                cur.exit_idx = i; cur.exit_price = xp; cur.realized_pnl = pnl
                cur.R_multiple = R; cur.bars_held = i-cur.entry_idx; cur.exit_time = t[i]
                trades.append(cur)
                in_pos = False; cur = None
            i += 1
    return trades


def run_bb(df, lots, mpu, *, use_50ema, bidir):
    n = len(df)
    if n < 150: return []
    c = df["close"].values; h = df["high"].values; l = df["low"].values; o = df["open"].values
    t = pd.to_datetime(df["time"]).values
    a = atr_wilder(df, 14).values
    s_ = pd.Series(c)
    mid = s_.rolling(20).mean().values
    std = s_.rolling(20).std().values
    upper = mid + 2*std; lower = mid - 2*std
    width = (upper - lower) / np.where(mid != 0, mid, 1)
    sq_thr = pd.Series(width).rolling(100).quantile(0.20).values
    in_sq = width <= sq_thr
    e50 = ema(df["close"], 50).values
    trades, in_pos, cur, i = [], False, None, 105
    while i < n:
        if not in_pos:
            if np.isnan(upper[i]): i += 1; continue
            recent = any(in_sq[max(0,i-5):i+1])
            if not recent: i += 1; continue
            went_long = False
            if c[i] > upper[i] and c[i-1] <= upper[i-1]:
                if use_50ema and not (c[i] > e50[i]): i += 1; continue
                slip = a[i] * SLIPPAGE
                cur = Trade(entry_idx=i, entry_price=c[i]+slip, initial_stop=lower[i],
                              entry_time=t[i], direction=1)
                in_pos = True; went_long = True
            if not went_long and bidir:
                if c[i] < lower[i] and c[i-1] >= lower[i-1]:
                    if use_50ema and not (c[i] < e50[i]): i += 1; continue
                    slip = a[i] * SLIPPAGE
                    cur = Trade(entry_idx=i, entry_price=c[i]-slip, initial_stop=upper[i],
                                  entry_time=t[i], direction=-1)
                    in_pos = True
            i += 1
        else:
            d = cur.direction
            if d == 1:
                trail = min(l[max(0,i-1):i]) if i > 0 else l[i-1]
                xp = None
                if l[i] <= cur.initial_stop: xp = cur.initial_stop
                elif o[i] < trail: xp = o[i]
                elif c[i] < trail: xp = c[i]
                if xp is not None:
                    xp -= a[i] * SLIPPAGE
                    pnl = (xp - cur.entry_price) * lots * mpu - 2 * COMMISSION
                    risk = (cur.entry_price - cur.initial_stop) * lots * mpu
                    R = pnl / risk if risk > 0 else 0
                    cur.exit_idx = i; cur.exit_price = xp; cur.realized_pnl = pnl
                    cur.R_multiple = R; cur.bars_held = i-cur.entry_idx; cur.exit_time = t[i]
                    trades.append(cur); in_pos = False; cur = None
            else:
                trail = max(h[max(0,i-1):i]) if i > 0 else h[i-1]
                xp = None
                if h[i] >= cur.initial_stop: xp = cur.initial_stop
                elif o[i] > trail: xp = o[i]
                elif c[i] > trail: xp = c[i]
                if xp is not None:
                    xp += a[i] * SLIPPAGE
                    pnl = (cur.entry_price - xp) * lots * mpu - 2 * COMMISSION
                    risk = (cur.initial_stop - cur.entry_price) * lots * mpu
                    R = pnl / risk if risk > 0 else 0
                    cur.exit_idx = i; cur.exit_price = xp; cur.realized_pnl = pnl
                    cur.R_multiple = R; cur.bars_held = i-cur.entry_idx; cur.exit_time = t[i]
                    trades.append(cur); in_pos = False; cur = None
            i += 1
    return trades


def main():
    print("="*130)
    print("COMPREHENSIVE REPORT — full per-cell stats with year-by-year P&L")
    print(f"Cost: ${COMMISSION}/trade + {SLIPPAGE}×ATR slip · "
          f"$100k starting balance")
    print("="*130)
    print()

    results = []
    for label, ticker, mpu, lots, kind in DEEP_DIVE:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists(): continue
        df = load_parquet(ppath)
        try:
            t0 = pd.to_datetime(df["time"].iloc[0])
            t1 = pd.to_datetime(df["time"].iloc[-1])
            span = (t1-t0).total_seconds()/86400
        except: span = len(df)
        if kind == "v4":      trades = run_v4(df, lots, mpu)
        elif kind == "v7":   trades = run_v7(df, lots, mpu)
        elif kind == "v8":   trades = run_v8(df, lots, mpu)
        elif kind == "bb_v1": trades = run_bb(df, lots, mpu, use_50ema=False, bidir=False)
        elif kind == "bb_v2": trades = run_bb(df, lots, mpu, use_50ema=True, bidir=False)
        else: continue
        s = compute_stats(trades, label=f"{label} {ticker}", span_days=span)
        results.append(s)

    # SKIP cells that never recovered
    survivors = [r for r in results if r.recovery_days is not None or r.max_dd_pct < 1]
    skipped = [r for r in results if r.recovery_days is None and r.max_dd_pct >= 1]

    survivors.sort(key=lambda r: -r.gross_pnl)

    print(f"📊 {len(survivors)} cells passed recovery filter, "
          f"{len(skipped)} skipped (never recovered)")
    print()

    # Skipped cells
    if skipped:
        print(f"⏭️  Skipped (never recovered DD):")
        for r in skipped:
            print(f"    {r.label} — DD {r.max_dd_pct:.1f}% never reclaimed")
        print()

    # Per-cell rich report
    for i, s in enumerate(survivors, 1):
        rec = "instant" if s.recovery_days == 0 else f"{s.recovery_days:.0f}d"
        print(f"━" * 130)
        print(f"#{i}  {s.label}")
        print(f"━" * 130)
        print(f"  ⏱  Span: {s.span_years:.1f} years  ·  Trades: {s.n}  "
              f"({s.trades_per_year:.1f}/yr)  ·  Avg hold: {s.avg_hold_days:.1f}d")
        print(f"  💰 Started ${s.starting:,.0f}  →  Ended ${s.ending:,.0f}  "
              f"(${s.gross_pnl:+,.0f}, {s.return_pct:+.2f}%)")
        print(f"  📈 PF {s.pf:.2f}  ·  WR {s.wr:.0f}% ({s.n_wins}W/{s.n_losses}L)  "
              f"·  R:R {s.rr:.2f}  ·  mean R {s.mean_R:+.3f}  ·  exp ${s.expectancy:+,.0f}/trade")
        print(f"  📉 Max DD: ${s.max_dd_usd:,.0f} ({s.max_dd_pct:.1f}%)  "
              f"·  DD lasted {s.dd_duration_days:.0f}d  "
              f"·  Recovered in {rec}")
        print(f"  🔁 Longest streak: {s.longest_win_streak}W / {s.longest_loss_streak}L  "
              f"·  Avg win ${s.avg_win:+,.0f}  ·  Avg loss ${s.avg_loss:+,.0f}")
        print(f"  📐 Sharpe {s.sharpe:.2f}  ·  Sortino {s.sortino:.2f}  "
              f"·  Calmar {s.calmar:.2f}")
        if s.yearly_pnl:
            print(f"  📅 Year-by-year P&L:")
            for yr in sorted(s.yearly_pnl.keys()):
                pnl = s.yearly_pnl[yr]
                bar_len = int(abs(pnl) / max(abs(p) for p in s.yearly_pnl.values()) * 30)
                bar = ("█" * bar_len) if pnl > 0 else ("░" * bar_len)
                print(f"      {yr}: ${pnl:>+10,.0f}  {bar}")
        print()

    # Quick scoreboard
    print("="*130)
    print("📋 SCOREBOARD (sorted by total $ gain, surviving cells only)")
    print("="*130)
    print(f"{'#':>3} {'Cell':46} {'Trades':>7} {'Total $':>10} "
          f"{'Yrs':>4} {'PF':>5} {'DD%':>5} {'RecDays':>8} "
          f"{'LongL':>5}")
    for i, s in enumerate(survivors, 1):
        rec = "instant" if s.recovery_days == 0 else (
            f"{s.recovery_days:.0f}d" if s.recovery_days else "—")
        print(f"{i:>3} {s.label[:46]:46} {s.n:>7} ${s.gross_pnl:>+9,.0f} "
              f"{s.span_years:>3.1f}y {s.pf:>5.2f} "
              f"{s.max_dd_pct:>4.1f}% {rec:>8} "
              f"{s.longest_loss_streak:>5}")


if __name__ == "__main__":
    main()
