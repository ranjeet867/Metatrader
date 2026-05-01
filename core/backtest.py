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
from typing import Literal, Optional

import numpy as np
import pandas as pd

from core.strategy import Signal


CloseReason = Literal["target", "stop", "end_of_data"]


@dataclass
class ClosedTrade:
    direction: str               # "LONG" or "SHORT"
    entry_bar_idx: int
    exit_bar_idx: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    lots: float
    money_per_unit_price: float  # for the symbol — how much $ moves per 1.0 price unit per 1 lot
    realized_pnl: float
    r_multiple: float            # realized_pnl / initial_dollar_risk
    initial_dollar_risk: float   # |entry - stop| × lots × money_per_unit_price
    close_reason: CloseReason


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

    @property
    def n_trades(self) -> int:
        return len(self.trades)


def run_backtest(
    candles: pd.DataFrame,
    signals: list[Signal],
    *,
    starting_balance: float,
    lots: float,
    money_per_unit_price: float,
    reconcile_tolerance: float = 0.01,
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

    # Index signals by bar_idx for fast lookup
    signals_by_bar = {s.bar_idx: s for s in signals if 0 <= s.bar_idx < n}

    balance = float(starting_balance)
    open_pos: Optional[Signal] = None     # the signal that opened the current position

    trades: list[ClosedTrade] = []
    equity_rows: list[dict] = []

    for i in range(n):
        # ----- 1. If we have an open position, check if SL or TP hit on THIS bar.
        if open_pos is not None:
            sig = open_pos
            hit_target = False
            hit_stop = False
            if sig.direction == "LONG":
                # Pessimistic: if both touched in same bar, assume SL hits first
                hit_stop = low[i] <= sig.stop_price
                hit_target = high[i] >= sig.target_price
            else:  # SHORT
                hit_stop = high[i] >= sig.stop_price
                hit_target = low[i] <= sig.target_price

            exit_price: Optional[float] = None
            close_reason: Optional[CloseReason] = None
            if hit_stop and hit_target:
                exit_price = sig.stop_price
                close_reason = "stop"
            elif hit_stop:
                exit_price = sig.stop_price
                close_reason = "stop"
            elif hit_target:
                exit_price = sig.target_price
                close_reason = "target"
            elif i == n - 1:
                # End of data — close at last close price
                exit_price = close[i]
                close_reason = "end_of_data"

            if exit_price is not None:
                # Compute PnL
                sign = 1.0 if sig.direction == "LONG" else -1.0
                pnl = (exit_price - sig.entry_price) * sign * lots * money_per_unit_price
                # Initial risk in dollars (always positive)
                init_risk = abs(sig.entry_price - sig.stop_price) * lots * money_per_unit_price
                r_multiple = pnl / init_risk if init_risk > 0 else 0.0

                trades.append(ClosedTrade(
                    direction=sig.direction,
                    entry_bar_idx=sig.bar_idx,
                    exit_bar_idx=i,
                    entry_price=sig.entry_price,
                    stop_price=sig.stop_price,
                    target_price=sig.target_price,
                    exit_price=exit_price,
                    lots=lots,
                    money_per_unit_price=money_per_unit_price,
                    realized_pnl=pnl,
                    r_multiple=r_multiple,
                    initial_dollar_risk=init_risk,
                    close_reason=close_reason,
                ))
                # *** THE FIX v1 was missing: actually update balance on close. ***
                balance += pnl
                open_pos = None

        # ----- 2. After resolving any close, can we open a new position on this bar?
        # We open at the close of the SIGNAL bar (so signal.bar_idx == i means open here).
        if open_pos is None and i in signals_by_bar:
            sig = signals_by_bar[i]
            # sanity-check the signal's geometry (LONG: stop<entry<target; SHORT: target<entry<stop)
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
            open_pos = sig

        # ----- 3. Mark-to-market equity at this bar's CLOSE
        if open_pos is not None:
            sign = 1.0 if open_pos.direction == "LONG" else -1.0
            floating = (close[i] - open_pos.entry_price) * sign * lots * money_per_unit_price
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
    )
