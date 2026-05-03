"""
backtest.py — single-position, reconciliation-enforced backtester.

Critical invariant (enforced in tests):
    sum(closed_trade.realized_pnl) == equity_curve[-1] - equity_curve[0]
    within $0.01

Design choices:
  - One position at a time. No concurrency, no pyramid, no partial closes.
  - Fixed-lots execution (the strategy provides entry/stop/target prices;
    sizing is handled outside the backtest core).
  - Bar-by-bar walk: each bar's high/low determines whether SL or TP hits.
  - On bar close, equity = balance + floating_pnl_of_open_trade.
  - When a trade closes, the realized PnL is ADDED TO BALANCE.
  - That last point is THE bug v1 had — fixed here by always updating balance.

Pessimistic intra-bar fills:
  - If a bar's range contains both SL and TP, assume SL fills first (worst case).
  - This is conservative and matches MT5 Strategy Tester's "Every tick" mode default.

Output: BacktestResult containing closed trades + equity curve + reconciliation status.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

import numpy as np
import pandas as pd

from core.indicators import atr_wilder
from core.strategy import Signal
from core.time_guards import (
    TimeGuardCfg,
    in_no_entry_window,
    needs_daily_flat,
    needs_weekend_flat,
)


CloseReason = Literal["target", "stop", "time", "end_of_data",
                       "weekend_flat", "daily_close_flat"]


@dataclass
class ClosedTrade:
    direction: str               # "LONG" or "SHORT"
    entry_bar_idx: int
    exit_bar_idx: int
    entry_price: float           # actual fill price (post-slippage)
    stop_price: float            # the stop level used for SL detection (unchanged)
    target_price: float          # the TP level used for TP detection (unchanged)
    exit_price: float            # actual fill price (post-slippage)
    lots: float
    money_per_unit_price: float  # for the symbol — how much $ moves per 1.0 price unit per 1 lot
    realized_pnl: float
    r_multiple: float            # realized_pnl / initial_dollar_risk
    initial_dollar_risk: float   # |entry - stop| × lots × money_per_unit_price (pre-slippage)
    close_reason: CloseReason
    signal_entry_price: float = 0.0   # pre-slippage entry from the signal (for reference)
    entry_slip: float = 0.0           # absolute price slip applied at entry (always >= 0)
    exit_slip: float = 0.0            # absolute price slip applied at exit  (always >= 0)


@dataclass
class BacktestResult:
    trades: list[ClosedTrade]
    equity_curve: pd.DataFrame   # cols: time, equity
    starting_balance: float
    ending_balance: float
    sum_realized_pnl: float
    equity_curve_pnl: float
    reconciles: bool             # |sum_pnl - eq_pnl| < 0.01
    reconcile_tolerance: float = 0.01
    skipped_signals: int = 0     # signals dropped because in_no_entry_window

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def reconcile_divergence(self) -> float:
        """Signed divergence in dollars: sum_realized - equity_pnl. INVARIANT-1
        is satisfied iff abs(divergence) < reconcile_tolerance."""
        return self.sum_realized_pnl - self.equity_curve_pnl


@dataclass
class PartitionMetrics:
    """Aggregate stats for a slice of trades (e.g. train vs test)."""
    label: str
    n_trades: int
    sum_pnl: float
    win_rate: float
    profit_factor: float
    avg_R: float
    bar_range: tuple[int, int]   # [first_bar_idx, last_bar_idx_inclusive]


def summarize_trades(trades: list[ClosedTrade], label: str,
                      bar_range: tuple[int, int]) -> PartitionMetrics:
    if not trades:
        return PartitionMetrics(label=label, n_trades=0, sum_pnl=0.0,
                                win_rate=0.0, profit_factor=0.0, avg_R=0.0,
                                bar_range=bar_range)
    n = len(trades)
    wins = [t for t in trades if t.realized_pnl > 0]
    losses = [t for t in trades if t.realized_pnl <= 0]
    gw = sum(t.realized_pnl for t in wins)
    gl = -sum(t.realized_pnl for t in losses)
    pf = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    avg_R = sum(t.r_multiple for t in trades) / n
    return PartitionMetrics(
        label=label, n_trades=n, sum_pnl=float(sum(t.realized_pnl for t in trades)),
        win_rate=len(wins) / n * 100.0, profit_factor=pf, avg_R=avg_R,
        bar_range=bar_range,
    )


def partition_train_test(result: BacktestResult, train_pct: float,
                          n_bars: int) -> tuple[PartitionMetrics, PartitionMetrics]:
    """Split closed trades into train/test by ENTRY bar.

    A trade with entry_bar_idx < split_idx is "train"; otherwise "test".
    Sum invariant: train.sum_pnl + test.sum_pnl == result.sum_realized_pnl.

    Args:
        result: a completed BacktestResult.
        train_pct: fraction of bars that go to train (e.g. 0.6 = first 60%).
                   Must be in (0.0, 1.0]. 1.0 puts everything in train.
        n_bars: total bars in the backtest (== len(candles)).
    """
    if not (0.0 < train_pct <= 1.0):
        raise ValueError(f"train_pct must be in (0, 1]; got {train_pct}")
    split_idx = int(n_bars * train_pct)
    train = [t for t in result.trades if t.entry_bar_idx < split_idx]
    test = [t for t in result.trades if t.entry_bar_idx >= split_idx]
    train_metrics = summarize_trades(train, label="train", bar_range=(0, split_idx - 1))
    test_metrics = summarize_trades(test, label="test", bar_range=(split_idx, n_bars - 1))
    return train_metrics, test_metrics


def apply_entry_slip(direction: str, entry_price: float, slip_abs: float) -> float:
    """Apply slippage AGAINST the trade at entry. Always slip_abs >= 0.

    LONG fills ABOVE the signal price; SHORT fills BELOW. This is the single
    source of truth — paper_executor and live_executor both call this.
    """
    return entry_price + slip_abs if direction == "LONG" else entry_price - slip_abs


def apply_exit_slip(direction: str, level_price: float, slip_abs: float) -> float:
    """Apply slippage AGAINST the trade at exit. Always slip_abs >= 0.

    LONG exits BELOW the SL/TP/close level; SHORT exits ABOVE. Mirror of entry.
    """
    return level_price - slip_abs if direction == "LONG" else level_price + slip_abs


def compute_realized_pnl(direction: str, actual_entry: float, actual_exit: float,
                          lots: float, money_per_unit_price: float,
                          commission_per_trade: float) -> float:
    """Single source-of-truth realized-PnL formula.

    INVARIANT-3: backtest, replay, paper, and live all compute realized PnL
    via THIS function. New code paths cannot define their own PnL formula.
    """
    sign = 1.0 if direction == "LONG" else -1.0
    gross = (actual_exit - actual_entry) * sign * lots * money_per_unit_price
    return gross - commission_per_trade


def compute_initial_dollar_risk(direction: str, signal_entry_price: float,
                                stop_price: float, lots: float,
                                money_per_unit_price: float) -> float:
    """Pre-slippage risk: |entry - stop| × lots × money_per_unit_price.

    Used to compute R-multiple. Identical on backtest / paper / live.
    """
    return abs(signal_entry_price - stop_price) * lots * money_per_unit_price


def run_backtest(
    candles: pd.DataFrame,
    signals: list[Signal],
    *,
    starting_balance: float,
    lots: float = 0.0,
    money_per_unit_price: float,
    commission_per_trade: float = 0.0,
    slippage_per_fill_atr_frac: float = 0.0,
    slippage_atr_period: int = 14,
    reconcile_tolerance: float = 0.01,
    # ----- INVARIANT-8: time-based forced exits (default OFF for backwards
    # compatibility with the existing 116 tests; opt-in per call site) -----
    symbol: str = "",
    enforce_weekend_flat: bool = False,
    enforce_daily_flat: bool = False,
    asset_class_overrides: dict | None = None,
    daily_close_flat_classes: tuple[str, ...] = ("stock", "index"),
    us_session_close_hhmm: str = "20:00",
    flat_buffer_minutes: int = 5,
    no_entry_minutes_before_close: int = 0,
    # ----- Phase 27: dynamic lot sizing.
    # If both risk_pct and symbol_info are provided, lots are recomputed
    # per-trade via core.position_sizer.calc_lots. Falls back to fixed
    # `lots` when either is None (backwards compat).
    risk_pct: float | None = None,
    symbol_info=None,
    sizing_uses_running_balance: bool = True,
) -> BacktestResult:
    """Run a single-position backtest.

    Args:
        candles: DataFrame with columns time, open, high, low, close, volume.
        signals: signals to act on. The backtester takes the FIRST signal whose
                 bar_idx > current_position_close_bar (one position at a time).
        starting_balance: initial account balance in account currency.
        lots: fixed lot size per trade.
        money_per_unit_price: $ that moves per 1.0 price unit per 1 lot.
                              e.g. for US100.cash with point_value=$1, this is $1.0.
                              The backtester is currency-agnostic; this number tells
                              it how to convert price moves to $.
        slippage_per_fill_atr_frac: every entry and every exit is slipped against
                              the trade by this fraction of ATR(slippage_atr_period)
                              at the FILL bar. LONG entry fills above signal price,
                              LONG exit fills below the SL/TP/close level (and mirror
                              for SHORT). 0.0 = no slippage. 0.1 = 10% of ATR per fill.
        slippage_atr_period: lookback for the slippage-reference ATR (Wilder).
                              Only computed when slippage > 0.

    Returns:
        BacktestResult with reconciliation invariants checked.
    """
    if candles.empty:
        return BacktestResult(
            trades=[], equity_curve=pd.DataFrame(columns=["time", "equity"]),
            starting_balance=starting_balance, ending_balance=starting_balance,
            sum_realized_pnl=0.0, equity_curve_pnl=0.0, reconciles=True,
        )

    n = len(candles)
    high = candles["high"].to_numpy()
    low = candles["low"].to_numpy()
    close = candles["close"].to_numpy()
    times = candles["time"].to_numpy()

    # ATR series for slippage (only computed when needed)
    if slippage_per_fill_atr_frac > 0.0:
        atr_series = atr_wilder(candles, slippage_atr_period).to_numpy()
    else:
        atr_series = np.zeros(n, dtype=float)

    # Index signals by bar_idx for fast lookup
    signals_by_bar = {s.bar_idx: s for s in signals if 0 <= s.bar_idx < n}

    # ----- INVARIANT-8 plumbing: detect timeframe + build TimeGuardCfg -----
    # tf_seconds is the median bar interval; used to project bar_close_time =
    # bar.time + tf_seconds. Only computed when at least one time-guard is on.
    time_guards_active = (enforce_weekend_flat or enforce_daily_flat
                            or no_entry_minutes_before_close > 0)
    tf_seconds = 0
    times_pd = candles["time"]   # tz-aware pandas Series
    if time_guards_active and n >= 2:
        diffs = times_pd.diff().dt.total_seconds().dropna().to_numpy()
        tf_seconds = int(np.median(diffs))

    tg_cfg = TimeGuardCfg(
        weekend_flat_all=enforce_weekend_flat,
        daily_close_flat_classes=daily_close_flat_classes,
        us_session_close_hhmm=us_session_close_hhmm,
        flat_buffer_minutes=flat_buffer_minutes,
        no_entry_minutes_before_close=no_entry_minutes_before_close,
        asset_class_overrides=asset_class_overrides,
    )

    def _bar_close_utc(i: int) -> datetime:
        """The UTC datetime that bar i closes at (== start of bar i+1)."""
        ts = times_pd.iloc[i]
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        return (ts + pd.Timedelta(seconds=tf_seconds)).to_pydatetime()

    balance = float(starting_balance)
    open_pos: Optional[Signal] = None     # the signal that opened the current position
    actual_entry: float = 0.0             # post-slippage fill price for the open position
    entry_slip_abs: float = 0.0           # |slip| at entry, recorded for the trade record
    current_lots: float = float(lots)     # dynamic per-trade when risk_pct is on

    trades: list[ClosedTrade] = []
    equity_rows: list[dict] = []
    skipped_signals = 0
    skipped_for_sizing = 0
    sizing_active = (risk_pct is not None and risk_pct > 0
                       and symbol_info is not None)

    def _close_at_bar(i: int, level_price: float, close_reason: CloseReason) -> None:
        """Close the currently-open position at bar i with the given level.

        Apply exit slippage AGAINST the trade. Append a ClosedTrade, update
        balance, clear open_pos / actual_entry / entry_slip_abs.
        """
        nonlocal balance, open_pos, actual_entry, entry_slip_abs, current_lots
        sig = open_pos
        assert sig is not None
        exit_slip_abs = atr_series[i] * slippage_per_fill_atr_frac
        if sig.direction == "LONG":
            actual_exit = level_price - exit_slip_abs
        else:
            actual_exit = level_price + exit_slip_abs

        sign = 1.0 if sig.direction == "LONG" else -1.0
        gross = (actual_exit - actual_entry) * sign * current_lots * money_per_unit_price
        pnl = gross - commission_per_trade
        init_risk = abs(sig.entry_price - sig.stop_price) * current_lots * money_per_unit_price
        r_multiple = pnl / init_risk if init_risk > 0 else 0.0

        trades.append(ClosedTrade(
            direction=sig.direction,
            entry_bar_idx=sig.bar_idx,
            exit_bar_idx=i,
            entry_price=actual_entry,
            stop_price=sig.stop_price,
            target_price=sig.target_price,
            exit_price=actual_exit,
            lots=current_lots,
            money_per_unit_price=money_per_unit_price,
            realized_pnl=pnl,
            r_multiple=r_multiple,
            initial_dollar_risk=init_risk,
            close_reason=close_reason,
            signal_entry_price=sig.entry_price,
            entry_slip=entry_slip_abs,
            exit_slip=exit_slip_abs,
        ))
        balance += pnl
        open_pos = None
        actual_entry = 0.0
        entry_slip_abs = 0.0

    for i in range(n):
        # ----- 1. INTRABAR close check: SL or TP hit on a position opened on a PRIOR bar.
        if open_pos is not None:
            sig = open_pos
            if sig.direction == "LONG":
                # Pessimistic: if both touched in same bar, assume SL hits first
                hit_stop = low[i] <= sig.stop_price
                hit_target = high[i] >= sig.target_price
            else:  # SHORT
                hit_stop = high[i] >= sig.stop_price
                hit_target = low[i] <= sig.target_price

            if hit_stop:
                _close_at_bar(i, sig.stop_price, "stop")
            elif hit_target:
                _close_at_bar(i, sig.target_price, "target")
            elif sig.max_hold_bars > 0 and i >= sig.bar_idx + sig.max_hold_bars:
                # Time-based exit: force-close at this bar's CLOSE.
                _close_at_bar(i, close[i], "time")

        # ----- 1b. INVARIANT-8: time-based forced flats (after SL/TP, before opens)
        # Weekend-flat takes precedence over daily-flat (Friday IS a weekday).
        if open_pos is not None and time_guards_active and tf_seconds > 0:
            bc_utc = _bar_close_utc(i)
            if enforce_weekend_flat and needs_weekend_flat(bc_utc, tg_cfg):
                _close_at_bar(i, close[i], "weekend_flat")
            elif (enforce_daily_flat and symbol
                  and needs_daily_flat(symbol, bc_utc, tg_cfg)):
                _close_at_bar(i, close[i], "daily_close_flat")

        # ----- 2. After resolving any intrabar close, open a new position on this bar?
        # We open at the close of the SIGNAL bar (so signal.bar_idx == i means open here).
        if open_pos is None and i in signals_by_bar:
            sig = signals_by_bar[i]
            # NO-ENTRY WINDOW: refuse to open if the bar's close-time falls
            # in the configured pre-close window (live-trading discipline).
            if (no_entry_minutes_before_close > 0 and tf_seconds > 0
                and in_no_entry_window(_bar_close_utc(i), tg_cfg)):
                skipped_signals += 1
            else:
                # sanity-check the signal's geometry
                if sig.direction == "LONG":
                    if not (sig.stop_price < sig.entry_price < sig.target_price):
                        raise ValueError(
                            f"bad LONG signal at bar {i}: stop={sig.stop_price}, "
                            f"entry={sig.entry_price}, target={sig.target_price}"
                        )
                else:
                    if not (sig.target_price < sig.entry_price < sig.stop_price):
                        raise ValueError(
                            f"bad SHORT signal at bar {i}: target={sig.target_price}, "
                            f"entry={sig.entry_price}, stop={sig.stop_price}"
                        )
                # ---- Phase 27: dynamic per-trade lot sizing ----
                if sizing_active:
                    from core.position_sizer import calc_lots
                    sizing_balance = balance if sizing_uses_running_balance else starting_balance
                    res = calc_lots(
                        equity=sizing_balance, risk_pct=float(risk_pct),
                        entry_price=sig.entry_price, stop_price=sig.stop_price,
                        sym=symbol_info,
                    )
                    if not res.ok:
                        skipped_for_sizing += 1
                        # Don't open this trade
                        # (proceed to step 3/4 with no position open)
                        # NOTE: we still consume the signal — backtest behaviour
                        # is "FIRST signal whose bar_idx > open close"; rejected
                        # signals do not block later signals.
                    else:
                        current_lots = res.lots
                        open_pos = sig
                        entry_slip_abs = atr_series[i] * slippage_per_fill_atr_frac
                        actual_entry = (sig.entry_price + entry_slip_abs
                                          if sig.direction == "LONG"
                                          else sig.entry_price - entry_slip_abs)
                else:
                    current_lots = float(lots)
                    open_pos = sig
                    entry_slip_abs = atr_series[i] * slippage_per_fill_atr_frac
                    if sig.direction == "LONG":
                        actual_entry = sig.entry_price + entry_slip_abs
                    else:
                        actual_entry = sig.entry_price - entry_slip_abs

        # ----- 3. END-OF-DATA close: any position still open on the last bar exits at close.
        # IMPORTANT: this runs AFTER step 2 so a same-bar entry+EOD-exit is fully accounted
        # for in this iteration (otherwise slippage at entry leaks into the equity curve as
        # unrealized floating PnL — see TestSlippage::test_eod_close_when_signal_at_last_bar).
        if open_pos is not None and i == n - 1:
            _close_at_bar(i, close[i], "end_of_data")

        # ----- 4. Mark-to-market equity at this bar's CLOSE
        if open_pos is not None:
            sign = 1.0 if open_pos.direction == "LONG" else -1.0
            floating = (close[i] - actual_entry) * sign * current_lots * money_per_unit_price
            equity = balance + floating
        else:
            equity = balance

        equity_rows.append({"time": times[i], "equity": equity})

    eq_df = pd.DataFrame(equity_rows)

    # ----- Reconciliation -----
    sum_realized = float(sum(t.realized_pnl for t in trades))
    eq_pnl = float(eq_df["equity"].iloc[-1] - starting_balance) if len(eq_df) else 0.0
    reconciles = abs(sum_realized - eq_pnl) < reconcile_tolerance

    return BacktestResult(
        trades=trades,
        equity_curve=eq_df,
        starting_balance=starting_balance,
        ending_balance=balance,
        sum_realized_pnl=sum_realized,
        equity_curve_pnl=eq_pnl,
        reconciles=reconciles,
        reconcile_tolerance=reconcile_tolerance,
        skipped_signals=skipped_signals,
    )
