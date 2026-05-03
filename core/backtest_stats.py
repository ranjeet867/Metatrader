"""
backtest_stats.py — full statistics suite computed from a BacktestResult.

The stock `BacktestResult` already has trades + equity_curve + reconciliation
fields. This module derives everything else a quant trader actually wants
to see when deciding whether to deploy a strategy:

  • n_trades, n_wins, n_losses, win_rate
  • profit factor, avg R, expectancy ($)
  • avg win, avg loss, **risk:reward ratio** (avg_win / |avg_loss|)
  • max consecutive wins, max consecutive losses
  • max drawdown $ + %  (peak-to-trough on the equity curve)
  • drawdown duration (days from peak to trough)
  • recovery duration (days from trough back to a new high)
  • sharpe-like ratio (mean / std of per-trade R)
  • CAGR-like return % (annualised on equity curve span)
  • median trade duration (bars)

Every metric is a pure function of (trades, equity_curve), so we test
them with synthetic inputs and the real backtest path stays unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import math

import pandas as pd


@dataclass(frozen=True)
class FullStats:
    # Headline counts
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate_pct: float

    # Profitability
    sum_realized: float
    profit_factor: float        # gross_wins / gross_losses; inf if no losses
    expectancy_dollars: float   # mean realized_pnl per trade
    avg_R: float                # mean r_multiple
    sharpe_R: float             # mean(R) / std(R), 0 if 1 trade or std=0

    # Win / loss anatomy
    avg_win_dollars: float      # mean of positive realized_pnl
    avg_loss_dollars: float     # mean of NEGATIVE-or-zero realized_pnl
    risk_reward_ratio: float    # avg_win / |avg_loss| (1.0 means symmetric)
    largest_win_dollars: float
    largest_loss_dollars: float

    # Streaks
    max_consec_wins: int
    max_consec_losses: int

    # Drawdown
    max_dd_dollars: float       # peak - trough on equity curve
    max_dd_pct: float           # vs peak
    max_dd_duration_days: float
    recovery_duration_days: float | None  # None if not recovered yet

    # Time / annualisation
    span_days: float
    cagr_pct: float | None      # None if span < 30 days

    # Trade-level pacing
    median_trade_bars: int
    avg_trade_bars: float

    @property
    def loss_rate_pct(self) -> float:
        return 100.0 - self.win_rate_pct


def _streak_max(seq: Iterable[bool]) -> int:
    """Length of the longest run of True in a sequence."""
    best = cur = 0
    for v in seq:
        if v:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return best


def _drawdown_picture(equity_curve: pd.DataFrame) -> tuple[
        float, float, float, float | None]:
    """Return (max_dd_$, max_dd_%, dd_duration_days, recovery_duration_days).

    dd_duration_days = days from peak to trough.
    recovery_duration_days = days from trough back to a NEW high above the
        original peak. None if the curve has not yet recovered.
    """
    if equity_curve is None or equity_curve.empty:
        return 0.0, 0.0, 0.0, None
    eq = equity_curve.copy()
    eq["time"] = pd.to_datetime(eq["time"], utc=True, errors="coerce")
    eq = eq.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    if eq.empty:
        return 0.0, 0.0, 0.0, None

    eq["peak"] = eq["equity"].cummax()
    eq["dd"] = eq["peak"] - eq["equity"]
    if eq["peak"].max() <= 0:
        return 0.0, 0.0, 0.0, None

    trough_idx = int(eq["dd"].idxmax())
    dd_dollars = float(eq["dd"].iloc[trough_idx])
    peak_value = float(eq["peak"].iloc[trough_idx])
    if dd_dollars <= 0 or peak_value <= 0:
        return 0.0, 0.0, 0.0, None
    dd_pct = dd_dollars / peak_value * 100.0

    # Find peak-time: the latest index ≤ trough_idx where equity equalled
    # the running peak (i.e., the moment the drawdown started).
    peak_idx = trough_idx
    while peak_idx >= 0 and eq["equity"].iloc[peak_idx] < peak_value:
        peak_idx -= 1
    peak_time = eq["time"].iloc[max(0, peak_idx)]
    trough_time = eq["time"].iloc[trough_idx]
    dd_duration = (trough_time - peak_time).total_seconds() / 86400.0

    # Recovery: first index AFTER trough where equity ≥ peak_value
    recovery = None
    after = eq.iloc[trough_idx + 1:]
    above = after[after["equity"] >= peak_value]
    if not above.empty:
        rec_time = above["time"].iloc[0]
        recovery = (rec_time - trough_time).total_seconds() / 86400.0

    return dd_dollars, dd_pct, max(0.0, dd_duration), recovery


def _cagr(equity_curve: pd.DataFrame, starting_balance: float
          ) -> tuple[float, float | None]:
    """Return (span_days, cagr_pct or None if span<30d)."""
    if equity_curve is None or equity_curve.empty or starting_balance <= 0:
        return 0.0, None
    eq = equity_curve.copy()
    eq["time"] = pd.to_datetime(eq["time"], utc=True, errors="coerce")
    eq = eq.dropna(subset=["time"]).sort_values("time")
    if len(eq) < 2:
        return 0.0, None
    span_s = (eq["time"].iloc[-1] - eq["time"].iloc[0]).total_seconds()
    span_days = span_s / 86400.0
    if span_days < 30:
        return span_days, None
    end_equity = float(eq["equity"].iloc[-1])
    if end_equity <= 0:
        return span_days, -100.0
    years = span_days / 365.25
    if years <= 0:
        return span_days, None
    growth = end_equity / starting_balance
    if growth <= 0:
        return span_days, -100.0
    cagr = (math.pow(growth, 1.0 / years) - 1.0) * 100.0
    return span_days, cagr


def compute_full_stats(result, *, starting_balance: float) -> FullStats:
    """The whole picture in one call. starting_balance is passed
    explicitly so the caller can simulate 'what if I started with $X'
    independently of result.starting_balance — useful for the dashboard
    backtest dialog where the user picks $100k as a yardstick."""
    trades = result.trades or []
    n = len(trades)

    pnls = [t.realized_pnl for t in trades]
    rs = [t.r_multiple for t in trades]
    wins_pnl = [p for p in pnls if p > 0]
    losses_pnl = [p for p in pnls if p <= 0]
    n_wins = len(wins_pnl)
    n_losses = len(losses_pnl)

    win_rate = (n_wins / n * 100.0) if n else 0.0
    sum_realized = float(sum(pnls))

    gw = float(sum(wins_pnl))
    gl = float(-sum(losses_pnl))
    if gl > 0:
        pf = gw / gl
    elif gw > 0:
        pf = float("inf")
    else:
        pf = 0.0

    expectancy = (sum_realized / n) if n else 0.0
    avg_R = (sum(rs) / n) if n else 0.0

    # Sharpe-on-R
    if n > 1:
        mean_r = avg_R
        var_r = sum((r - mean_r) ** 2 for r in rs) / (n - 1)
        std_r = math.sqrt(var_r)
        sharpe_R = (mean_r / std_r) if std_r > 0 else 0.0
    else:
        sharpe_R = 0.0

    avg_win_d = (gw / n_wins) if n_wins else 0.0
    avg_loss_d = (-gl / n_losses) if n_losses else 0.0
    rr_ratio = (avg_win_d / abs(avg_loss_d)) if avg_loss_d != 0 else (
        float("inf") if avg_win_d > 0 else 0.0)
    largest_win = max(wins_pnl) if wins_pnl else 0.0
    largest_loss = min(losses_pnl) if losses_pnl else 0.0

    # Streaks
    max_consec_wins = _streak_max((p > 0) for p in pnls)
    max_consec_losses = _streak_max((p <= 0) for p in pnls)

    # Drawdown
    dd_d, dd_p, dd_days, rec_days = _drawdown_picture(result.equity_curve)

    # Span / CAGR
    span_days, cagr = _cagr(result.equity_curve, starting_balance)

    # Trade pacing
    bars = [t.exit_bar_idx - t.entry_bar_idx for t in trades]
    median_bars = int(sorted(bars)[len(bars) // 2]) if bars else 0
    avg_bars = (sum(bars) / len(bars)) if bars else 0.0

    return FullStats(
        n_trades=n, n_wins=n_wins, n_losses=n_losses,
        win_rate_pct=win_rate,
        sum_realized=sum_realized,
        profit_factor=pf, expectancy_dollars=expectancy,
        avg_R=avg_R, sharpe_R=sharpe_R,
        avg_win_dollars=avg_win_d, avg_loss_dollars=avg_loss_d,
        risk_reward_ratio=rr_ratio,
        largest_win_dollars=largest_win,
        largest_loss_dollars=largest_loss,
        max_consec_wins=max_consec_wins,
        max_consec_losses=max_consec_losses,
        max_dd_dollars=dd_d, max_dd_pct=dd_p,
        max_dd_duration_days=dd_days,
        recovery_duration_days=rec_days,
        span_days=span_days, cagr_pct=cagr,
        median_trade_bars=median_bars,
        avg_trade_bars=avg_bars,
    )


def rescale_to_starting_balance(result, target_starting: float):
    """Return a new equity-curve DataFrame scaled so it starts at
    `target_starting`. Used by the dashboard so the user can see "what
    would $100k have looked like" without re-running the backtest."""
    eq = result.equity_curve
    if eq is None or eq.empty:
        return eq
    bal = result.starting_balance
    if bal <= 0:
        return eq
    factor = target_starting / bal
    out = eq.copy()
    out["equity"] = (eq["equity"].astype(float) - bal) * factor + target_starting
    return out
