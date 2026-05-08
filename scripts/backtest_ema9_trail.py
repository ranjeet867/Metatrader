#!/usr/bin/env python3
"""
backtest_ema9_trail.py — User-specified 9-EMA pullback strategy with
prior-day-low trailing stop. Custom backtest because the trailing
exit doesn't fit the standard static-SL engine.

Spec (user description):
  Entry:
    1. Last 3 daily candles closed ABOVE the 9-EMA (D1)
    2. Break of the highest of the last 3 candles' highs
    3. Enter at the close of the breakout candle

  Initial stop:
    Lowest of the last 3 candles' lows × (1 − 0.001)  (0.10% extra)

  Trailing exit:
    On EACH new daily candle:
      - If close < prior day's low → EXIT at close
      - If open < prior day's low (gap-down) → EXIT at open

  Re-entry:
    Same setup conditions — strategy can re-arm after exit

Tested on:
  US100.cash D1, US500.cash D1, XAUUSD D1, XAGUSD D1

Cost:
  $4 commission, 0.05×ATR slippage (matches catalog cost-priced sweep)

Output:
  Per-instrument: PF, WR, R:R, n_trades, mean_R, max_dd, avg_hold_days
  Combined verdict: deploy_safe vs the catalog hard gates
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from core.data import load_parquet
from core.indicators import ema, atr_wilder
from core import cost_defaults

# Per-ticker config — matches sweep_grid + sweep_tsmom conventions
TICKERS = [
    ("US100.cash", 1.0,    6.5),    # ($/unit, default lots)
    ("US500.cash", 1.0,    5.0),
    ("XAUUSD",     100.0,  0.30),
    ("XAGUSD",     5_000.0, 0.40),
]

# Strategy params (user-spec, no per-cell tuning)
EMA_PERIOD = 9
LOOKBACK_BARS = 3              # last 3 daily candles
STOP_BUFFER_PCT = 0.001        # 0.10%
COMMISSION_USD = cost_defaults.DEFAULT_COMMISSION_USD
SLIPPAGE_ATR_FRAC = cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC


@dataclass
class Trade:
    entry_idx: int
    entry_price: float
    initial_stop: float
    exit_idx: int = -1
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    R_multiple: float = 0.0
    bars_held: int = 0
    exit_reason: str = ""


def run_backtest(df: pd.DataFrame, *, lots: float, mpu: float,
                 starting_balance: float = 100_000.0) -> list[Trade]:
    """Walk through D1 bars and execute the strategy with trailing exit."""
    n = len(df)
    if n < 50:
        return []

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    opens = df["open"].values
    ema_vals = ema(df["close"], EMA_PERIOD).values
    atr_vals = atr_wilder(df, 14).values

    trades: list[Trade] = []
    in_pos = False
    cur: Optional[Trade] = None
    bar_idx = LOOKBACK_BARS + EMA_PERIOD + 5  # warmup

    while bar_idx < n:
        if not in_pos:
            # Check entry conditions on bar i (signal generated at close)
            i = bar_idx
            # Condition 1: last 3 closes > 9EMA (using bars i-3, i-2, i-1)
            if i < 3:
                bar_idx += 1
                continue
            cond1 = all(
                closes[i - k] > ema_vals[i - k]
                for k in (1, 2, 3)
                if not np.isnan(ema_vals[i - k])
            )
            if not cond1:
                bar_idx += 1
                continue
            # Condition 2: break of 3-day high — current bar's close exceeds
            # max(high[i-3], high[i-2], high[i-1])
            three_day_high = max(highs[i - 1], highs[i - 2], highs[i - 3])
            if closes[i] <= three_day_high:
                bar_idx += 1
                continue
            # Entry triggered
            three_day_low = min(lows[i - 1], lows[i - 2], lows[i - 3])
            stop_price = three_day_low * (1.0 - STOP_BUFFER_PCT)
            # Slip the entry adverse (LONG fills above signal)
            slip_abs = atr_vals[i] * SLIPPAGE_ATR_FRAC
            entry_price = closes[i] + slip_abs
            cur = Trade(
                entry_idx=i,
                entry_price=entry_price,
                initial_stop=stop_price,
            )
            in_pos = True
            bar_idx += 1
        else:
            assert cur is not None
            j = bar_idx
            prev_low = lows[j - 1]
            exit_price = None
            exit_reason = ""

            # 1. Initial stop hit during this bar?
            if lows[j] <= cur.initial_stop:
                exit_price = cur.initial_stop  # filled at SL
                exit_reason = "initial_stop"
            # 2. Gap-down below prior day's low at OPEN?
            elif opens[j] < prev_low:
                exit_price = opens[j]
                exit_reason = "gap_below_prev_low"
            # 3. Close below prior day's low?
            elif closes[j] < prev_low:
                exit_price = closes[j]
                exit_reason = "close_below_prev_low"

            if exit_price is not None:
                # Slip exit adverse (LONG sells below)
                slip_abs = atr_vals[j] * SLIPPAGE_ATR_FRAC
                exit_price -= slip_abs
                # PnL — long position
                price_move = exit_price - cur.entry_price
                pnl = price_move * lots * mpu - 2 * COMMISSION_USD  # entry + exit
                # R-multiple — risk = entry − initial_stop
                risk_per_unit = cur.entry_price - cur.initial_stop
                risk_dollars = risk_per_unit * lots * mpu
                R = pnl / risk_dollars if risk_dollars > 0 else 0.0
                cur.exit_idx = j
                cur.exit_price = exit_price
                cur.realized_pnl = pnl
                cur.R_multiple = R
                cur.bars_held = j - cur.entry_idx
                cur.exit_reason = exit_reason
                trades.append(cur)
                in_pos = False
                cur = None

            bar_idx += 1

    return trades


def stats(trades: list[Trade], total_bars: int, span_days: float) -> dict:
    if not trades:
        return dict(n=0, pf=0, wr=0, mean_R=0, rr=0, max_dd_pct=0,
                       trades_per_year=0, exit_reasons={})
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl < 0]
    gp = sum(t.realized_pnl for t in wins)
    gl = -sum(t.realized_pnl for t in losses)
    pf = (gp / gl) if gl > 0 else (9.99 if gp > 0 else 0.0)
    wr = len(wins) / len(trades) * 100
    mean_R = sum(t.R_multiple for t in trades) / len(trades)
    avg_w = sum(t.R_multiple for t in wins) / len(wins) if wins else 0
    avg_l = -sum(t.R_multiple for t in losses) / len(losses) if losses else 0
    rr = avg_w / avg_l if avg_l > 0 else 0
    # Max DD
    eq = [0.0]
    for t in trades:
        eq.append(eq[-1] + t.realized_pnl)
    eq_arr = pd.Series(eq)
    peak = eq_arr.cummax()
    dd_dollars = (peak - eq_arr).max()
    starting_balance = 100_000
    max_dd_pct = dd_dollars / starting_balance * 100
    # Frequency
    years = span_days / 365.0
    trades_per_year = len(trades) / years if years > 0 else 0
    # Exit-reason breakdown
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    avg_hold = sum(t.bars_held for t in trades) / len(trades)
    return dict(
        n=len(trades), pf=pf, wr=wr, mean_R=mean_R, rr=rr,
        max_dd_pct=max_dd_pct,
        trades_per_year=trades_per_year,
        exit_reasons=reasons,
        avg_hold_days=avg_hold,
        gross_pnl=gp - gl,
    )


def main():
    print("=" * 100)
    print("EMA9 TRAIL backtest — 9-EMA D1 pullback with prior-day-low "
          "trailing stop")
    print(f"Cost: ${COMMISSION_USD}/trade, {SLIPPAGE_ATR_FRAC}×ATR slip · "
          f"3-bar high break entry · 0.10% stop buffer")
    print("=" * 100)
    print()
    print(f"{'Ticker':12} {'Bars':>6} {'n_tr':>4} {'PF':>5} {'WR%':>4} "
          f"{'R:R':>5} {'mean_R':>7} {'DD%':>5} {'tr/yr':>5} {'avgHold':>7}")
    print("-" * 80)

    overall_trades = []
    for ticker, mpu, lots in TICKERS:
        ppath = ROOT / "data" / f"{ticker}_D1.parquet"
        if not ppath.exists():
            print(f"{ticker:12} (no parquet)")
            continue
        df = load_parquet(ppath)
        try:
            t_first = pd.to_datetime(df["time"].iloc[0])
            t_last = pd.to_datetime(df["time"].iloc[-1])
            span = (t_last - t_first).total_seconds() / 86400
        except Exception:
            span = len(df)
        trades = run_backtest(df, lots=lots, mpu=mpu)
        s = stats(trades, len(df), span)
        if s["n"] > 0:
            print(f"{ticker[:12]:12} {len(df):>6} {s['n']:>4} "
                  f"{s['pf']:>5.2f} {s['wr']:>3.0f}% {s['rr']:>5.2f} "
                  f"{s['mean_R']:>+7.2f} {s['max_dd_pct']:>4.1f}% "
                  f"{s['trades_per_year']:>5.1f} {s['avg_hold_days']:>6.1f}d")
            print(f"{'':12} exit reasons: {s['exit_reasons']}")
        else:
            print(f"{ticker[:12]:12} {len(df):>6}    0  no trades")
        overall_trades.extend(trades)

    # Combined summary
    print()
    print("=" * 80)
    if overall_trades:
        total_n = len(overall_trades)
        wins = [t for t in overall_trades if t.realized_pnl > 0]
        gp = sum(t.realized_pnl for t in wins)
        gl = -sum(t.realized_pnl for t in overall_trades if t.realized_pnl < 0)
        pf = gp / gl if gl > 0 else 9.99
        wr = len(wins) / total_n * 100
        mean_R = sum(t.R_multiple for t in overall_trades) / total_n
        print(f"COMBINED: {total_n} trades · PF {pf:.2f} · WR {wr:.0f}% · "
              f"mean R {mean_R:+.2f}")

        # Catalog-style hard-gate verdict
        from core import edge_catalog
        deploy_safe = (
            pf >= 1.05 and total_n >= 15 and mean_R > 0
        )
        print(f"deploy_safe (combined): "
              f"{'✓ would PASS gates' if deploy_safe else '✗ FAILS gates'}")
    else:
        print("(no trades across all tickers)")


if __name__ == "__main__":
    main()
