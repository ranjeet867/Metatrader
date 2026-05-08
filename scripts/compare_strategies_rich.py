#!/usr/bin/env python3
"""
compare_strategies_rich.py — re-run top cells with rich metrics for
side-by-side comparison.

Computes for each cell:
  - n_trades, n_wins, n_losses
  - starting_balance, ending_balance, $gain, %gain
  - max_dd_pct, max_dd_$, dd_duration_days, recovery_days
  - longest_winning_streak, longest_losing_streak
  - avg_win$, avg_loss$, expectancy_per_trade
  - PF, WR, R:R, mean_R
  - avg_hold_days, span_years, trades_per_year
  - Sharpe (R-based), Sortino (R-based), Calmar (annualised return / max DD%)

Cells compared:
  - All ema9_trail variants (V1, V2, V3, V4, V7) on XAUUSD/XAGUSD/GER40/XPDUSD
  - For reference: existing catalog cells rsi_30_70 EURUSD M15,
    ema_cross_9_20 US100 M15, tsmom JP225 D1
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core import cost_defaults
from core.data import load_parquet
from core.indicators import ema, atr_wilder, rsi_wilder

STARTING_BALANCE = 100_000.0
COMMISSION_USD = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE_ATR_FRAC = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC
RSI_THRESHOLD = 55.0
LOOKBACK_BARS = 3
STOP_BUFFER_PCT = 0.001


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


def run_ema9_trail(df: pd.DataFrame, *, lots: float, mpu: float,
                     use_50ema: bool = False, trail_lookback: int = 1,
                     use_rsi_gate: bool = False) -> list[Trade]:
    n = len(df)
    if n < 60:
        return []
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    times = pd.to_datetime(df["time"]).values
    ema9 = ema(df["close"], 9).values
    ema50 = ema(df["close"], 50).values
    rsi = rsi_wilder(df["close"], 14).values
    atr = atr_wilder(df, 14).values

    trades: list[Trade] = []
    in_pos = False
    cur: Optional[Trade] = None
    bar_idx = 60

    while bar_idx < n:
        if not in_pos:
            i = bar_idx
            cond1 = all(closes[i-k] > ema9[i-k] for k in (1,2,3) if not np.isnan(ema9[i-k]))
            if use_50ema:
                cond1 = cond1 and all(closes[i-k] > ema50[i-k] for k in (1,2,3) if not np.isnan(ema50[i-k]))
            if use_rsi_gate:
                cond1 = cond1 and all(rsi[i-k] > RSI_THRESHOLD for k in (1,2,3) if not np.isnan(rsi[i-k]))
            if not cond1:
                bar_idx += 1; continue
            three_high = max(highs[i-1], highs[i-2], highs[i-3])
            if closes[i] <= three_high:
                bar_idx += 1; continue
            three_low = min(lows[i-1], lows[i-2], lows[i-3])
            stop = three_low * (1.0 - STOP_BUFFER_PCT)
            slip = atr[i] * SLIPPAGE_ATR_FRAC
            cur = Trade(entry_idx=i, entry_price=closes[i] + slip,
                          initial_stop=stop, entry_time=times[i])
            in_pos = True
            bar_idx += 1
        else:
            j = bar_idx
            tl = max(0, j - trail_lookback)
            trail_low = min(lows[tl:j]) if j > tl else lows[j-1]
            exit_p = None
            if lows[j] <= cur.initial_stop:
                exit_p = cur.initial_stop
            elif opens[j] < trail_low:
                exit_p = opens[j]
            elif closes[j] < trail_low:
                exit_p = closes[j]
            if exit_p is not None:
                slip = atr[j] * SLIPPAGE_ATR_FRAC
                exit_p -= slip
                pnl = (exit_p - cur.entry_price) * lots * mpu - 2 * COMMISSION_USD
                risk = (cur.entry_price - cur.initial_stop) * lots * mpu
                R = pnl / risk if risk > 0 else 0.0
                cur.exit_idx = j
                cur.exit_price = exit_p
                cur.realized_pnl = pnl
                cur.R_multiple = R
                cur.bars_held = j - cur.entry_idx
                cur.exit_time = times[j]
                trades.append(cur)
                in_pos = False
                cur = None
            bar_idx += 1
    return trades


@dataclass
class RichMetrics:
    label: str = ""
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    starting_balance: float = STARTING_BALANCE
    ending_balance: float = STARTING_BALANCE
    gross_pnl: float = 0.0
    return_pct: float = 0.0
    pf: float = 0.0
    win_rate_pct: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    expectancy_usd: float = 0.0
    rr_ratio: float = 0.0
    mean_R: float = 0.0
    max_dd_pct: float = 0.0
    max_dd_usd: float = 0.0
    dd_duration_days: float = 0.0
    recovery_days: Optional[float] = None
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    avg_hold_days: float = 0.0
    span_years: float = 0.0
    trades_per_year: float = 0.0
    sharpe_R: float = 0.0
    sortino_R: float = 0.0
    calmar: float = 0.0


def compute_rich_metrics(trades: list[Trade], *,
                            label: str,
                            span_days: float) -> RichMetrics:
    m = RichMetrics(label=label)
    if not trades:
        return m
    m.n_trades = len(trades)
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    m.n_wins = len(wins)
    m.n_losses = len(losses)
    m.gross_pnl = sum(t.realized_pnl for t in trades)
    m.ending_balance = m.starting_balance + m.gross_pnl
    m.return_pct = m.gross_pnl / m.starting_balance * 100
    gp = sum(t.realized_pnl for t in wins)
    gl = -sum(t.realized_pnl for t in losses)
    m.pf = gp / gl if gl > 0 else (9.99 if gp > 0 else 0)
    m.win_rate_pct = m.n_wins / m.n_trades * 100
    m.avg_win_usd = gp / m.n_wins if m.n_wins else 0
    m.avg_loss_usd = -gl / m.n_losses if m.n_losses else 0
    m.expectancy_usd = m.gross_pnl / m.n_trades
    m.rr_ratio = (m.avg_win_usd / abs(m.avg_loss_usd)
                    if m.avg_loss_usd else 0)
    m.mean_R = sum(t.R_multiple for t in trades) / m.n_trades
    m.avg_hold_days = sum(t.bars_held for t in trades) / m.n_trades
    m.span_years = span_days / 365.25
    m.trades_per_year = m.n_trades / m.span_years if m.span_years > 0 else 0

    # Equity curve + DD
    eq_vals = [m.starting_balance]
    for t in trades:
        eq_vals.append(eq_vals[-1] + t.realized_pnl)
    eq_arr = pd.Series(eq_vals)
    peak = eq_arr.cummax()
    dd_dollars_series = peak - eq_arr
    max_dd_idx = int(dd_dollars_series.idxmax())
    m.max_dd_usd = float(dd_dollars_series.iloc[max_dd_idx])
    m.max_dd_pct = m.max_dd_usd / m.starting_balance * 100
    # DD duration: bars from peak preceding trough to trough
    pre_peak_idx = int(peak.iloc[:max_dd_idx + 1].idxmax())
    if max_dd_idx > pre_peak_idx and max_dd_idx > 0:
        try:
            pre_t = pd.to_datetime(trades[max(0, pre_peak_idx - 1)].exit_time)
            tr_t = pd.to_datetime(trades[max(0, max_dd_idx - 1)].exit_time)
            m.dd_duration_days = max(0, (tr_t - pre_t).total_seconds() / 86400)
        except Exception:
            m.dd_duration_days = 0.0
    # Recovery: from trough to next peak reclaim
    if m.max_dd_usd > 0.01 and max_dd_idx < len(eq_vals) - 1:
        peak_value = peak.iloc[max_dd_idx]
        rec_idx = None
        for k in range(max_dd_idx, len(eq_vals)):
            if eq_vals[k] >= peak_value:
                rec_idx = k
                break
        if rec_idx is not None and rec_idx > max_dd_idx:
            try:
                tr_t = pd.to_datetime(trades[max(0, max_dd_idx - 1)].exit_time)
                rec_t = pd.to_datetime(trades[min(rec_idx - 1, len(trades) - 1)].exit_time)
                m.recovery_days = max(0, (rec_t - tr_t).total_seconds() / 86400)
            except Exception:
                m.recovery_days = float(rec_idx - max_dd_idx)
        else:
            m.recovery_days = None  # Never recovered

    # Streaks
    cur_w = cur_l = 0
    for t in trades:
        if t.realized_pnl > 0:
            cur_w += 1
            m.longest_win_streak = max(m.longest_win_streak, cur_w)
            cur_l = 0
        elif t.realized_pnl < 0:
            cur_l += 1
            m.longest_loss_streak = max(m.longest_loss_streak, cur_l)
            cur_w = 0
        else:
            cur_w = cur_l = 0

    # Sharpe (R-based) — mean / stdev of trade R-multiples × sqrt(trades_per_year)
    R_series = [t.R_multiple for t in trades]
    if len(R_series) > 1:
        mean_R_arr = float(np.mean(R_series))
        std_R = float(np.std(R_series, ddof=1))
        if std_R > 0:
            m.sharpe_R = mean_R_arr / std_R * math.sqrt(max(m.trades_per_year, 1))
        # Sortino — use only downside deviation
        neg_R = [r for r in R_series if r < 0]
        if neg_R and len(neg_R) > 1:
            downside_std = float(np.std(neg_R, ddof=1))
            if downside_std > 0:
                m.sortino_R = mean_R_arr / downside_std * math.sqrt(max(m.trades_per_year, 1))

    # Calmar — annualised return / max DD%
    if m.max_dd_pct > 0 and m.span_years > 0:
        annualised = m.return_pct / m.span_years
        m.calmar = annualised / m.max_dd_pct
    return m


CELLS_TO_TEST = [
    # (variant_label, ticker, mpu, lots, params)
    ("V7 (50EMA+2bar+RSI55)", "XAGUSD",     5_000.0, 0.40,
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=True)),
    ("V4 (50EMA+2bar)",       "XAUUSD",     100.0,   0.30,
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=False)),
    ("V4 (50EMA+2bar)",       "XAGUSD",     5_000.0, 0.40,
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=False)),
    ("V4 (50EMA+2bar)",       "GER40.cash", 1.0,     3.0,
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=False)),
    ("V7 (RSI55)",            "XAUUSD",     100.0,   0.30,
     dict(use_50ema=True, trail_lookback=2, use_rsi_gate=True)),
    ("V2 (50EMA only)",       "XPDUSD",     100.0,   0.20,
     dict(use_50ema=True, trail_lookback=1, use_rsi_gate=False)),
    ("V1 (baseline)",         "XAUUSD",     100.0,   0.30,
     dict(use_50ema=False, trail_lookback=1, use_rsi_gate=False)),
]


def main():
    print("=" * 130)
    print("RICH METRICS COMPARISON — ema9_trail variants on D1")
    print(f"Starting balance: ${STARTING_BALANCE:,.0f}  ·  "
          f"Cost: ${COMMISSION_USD}/trade + {SLIPPAGE_ATR_FRAC}×ATR slip")
    print("=" * 130)

    results: list[RichMetrics] = []
    for label, ticker, mpu, lots, params in CELLS_TO_TEST:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists():
            continue
        df = load_parquet(ppath)
        try:
            t_first = pd.to_datetime(df["time"].iloc[0])
            t_last = pd.to_datetime(df["time"].iloc[-1])
            span = (t_last - t_first).total_seconds() / 86400
        except Exception:
            span = len(df)
        trades = run_ema9_trail(df, lots=lots, mpu=mpu, **params)
        full_label = f"{label} {ticker}"
        m = compute_rich_metrics(trades, label=full_label, span_days=span)
        results.append(m)

    # Sort by gross_pnl desc
    results.sort(key=lambda x: -x.gross_pnl)

    # Output table 1: P&L summary
    print(f"\n{'Cell':38} {'n':>4} {'WR%':>4} {'$gain':>10} {'%gain':>7} "
          f"{'end$':>11} {'PF':>5} {'mean_R':>7}")
    print("-" * 130)
    for m in results:
        print(f"{m.label[:38]:38} {m.n_trades:>4} "
              f"{m.win_rate_pct:>3.0f}% ${m.gross_pnl:>+9,.0f} "
              f"{m.return_pct:>+6.2f}% ${m.ending_balance:>10,.0f} "
              f"{m.pf:>5.2f} {m.mean_R:>+7.3f}")

    # Output table 2: Drawdown + recovery
    print(f"\n{'Cell':38} {'maxDD%':>6} {'maxDD$':>10} {'DDdays':>7} "
          f"{'recovery':>10} {'longestL':>9} {'avgHold':>8}")
    print("-" * 130)
    for m in results:
        rec = "never" if m.recovery_days is None else f"{m.recovery_days:.0f}d"
        print(f"{m.label[:38]:38} {m.max_dd_pct:>5.2f}% "
              f"${m.max_dd_usd:>9,.0f} {m.dd_duration_days:>6.0f}d "
              f"{rec:>10} {m.longest_loss_streak:>9} "
              f"{m.avg_hold_days:>6.1f}d")

    # Output table 3: Risk-adjusted metrics
    print(f"\n{'Cell':38} {'Sharpe':>7} {'Sortino':>8} {'Calmar':>7} "
          f"{'avgWin$':>9} {'avgLoss$':>10} {'span_yr':>7} {'tr/yr':>5}")
    print("-" * 130)
    for m in results:
        print(f"{m.label[:38]:38} {m.sharpe_R:>7.2f} {m.sortino_R:>8.2f} "
              f"{m.calmar:>7.2f} ${m.avg_win_usd:>+8,.0f} "
              f"${m.avg_loss_usd:>+9,.0f} {m.span_years:>6.1f}y "
              f"{m.trades_per_year:>5.1f}")

    # Verdict — best by combined metrics
    print()
    print("=" * 130)
    print("VERDICT: top 3 by risk-adjusted score (PF × mean_R × Sortino)")
    print("=" * 130)
    scored = sorted(
        results,
        key=lambda m: -(m.pf * max(m.mean_R, 0.01) * max(m.sortino_R, 0.1))
    )
    for i, m in enumerate(scored[:3], 1):
        rec = "never" if m.recovery_days is None else f"{m.recovery_days:.0f}d"
        print(f"\n#{i}  {m.label}")
        print(f"    Started ${m.starting_balance:,.0f} → ${m.ending_balance:,.0f}  "
              f"({m.return_pct:+.2f}% over {m.span_years:.1f} years)")
        print(f"    {m.n_trades} trades · {m.win_rate_pct:.0f}% WR · "
              f"PF {m.pf:.2f} · expectancy ${m.expectancy_usd:+,.0f}/trade")
        print(f"    Worst drawdown: ${m.max_dd_usd:,.0f} "
              f"({m.max_dd_pct:.1f}% of starting balance), "
              f"lasted {m.dd_duration_days:.0f}d, recovered in {rec}")
        print(f"    Longest losing streak: {m.longest_loss_streak} trades · "
              f"Sharpe {m.sharpe_R:.2f} · Sortino {m.sortino_R:.2f} · "
              f"Calmar {m.calmar:.2f}")


if __name__ == "__main__":
    main()
