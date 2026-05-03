"""
replay.py — bar-by-bar replay using PaperExecutor as the engine.

Architectural choice: replay is a SHADOW of run_backtest. It computes the
signals ONCE on the full candle history (just like the backtester) and then
walks bar-by-bar using PaperExecutor for state. Both paths route money math
through core.backtest helpers (apply_*_slip, compute_realized_pnl), so parity
is by-construction — verified per-trade by tests/test_replay_parity.py.

Why precompute signals instead of calling strategy.signals(view) at each tick?
Several strategies guard on `len(candles) < period + 2: return []`. On a
walk-forward view with only k bars, that guard rejects the same bar that
the full-candles call would emit a signal on. The replay's job is to
PROVE that the production engine reproduces the backtest's trades; live mode
will always have enough history at the moment of a signal, so this is purely
a parity-of-engine concern.

Paper / live use a different code path (runner.tick), which is the right
choice because they actually do receive bars one-at-a-time.

INVARIANT-2: replay-parity. INVARIANT-3: imports core.backtest helpers.
INVARIANT-8: time-guard cfg respected when supplied.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.backtest import ClosedTrade
from core.indicators import atr_wilder
from core.paper_executor import Bar, PaperExecutor
from core.strategy import Strategy
from core.time_guards import TimeGuardCfg, in_no_entry_window


@dataclass
class ReplayResult:
    trades: list[ClosedTrade]
    equity_curve: pd.DataFrame
    starting_balance: float
    ending_balance: float
    sum_realized_pnl: float
    equity_curve_pnl: float
    reconciles: bool
    reconcile_tolerance: float = 0.01
    skipped_signals: int = 0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def reconcile_divergence(self) -> float:
        return self.sum_realized_pnl - self.equity_curve_pnl


def replay_run(
    candles: pd.DataFrame,
    strategy: Strategy,
    *,
    symbol: str,
    tf: str,
    starting_balance: float,
    lots: float = 0.0,
    money_per_unit_price: float,
    commission_per_trade: float = 0.0,
    slippage_per_fill_atr_frac: float = 0.0,
    slippage_atr_period: int = 14,
    time_guard_cfg: TimeGuardCfg | None = None,
    reconcile_tolerance: float = 0.01,
    # ----- Phase 27: dynamic sizing for replay parity with sized backtests
    risk_pct: float | None = None,
    symbol_info=None,
    sizing_uses_running_balance: bool = True,
) -> ReplayResult:
    n = len(candles)
    if n == 0:
        return ReplayResult(
            trades=[], equity_curve=pd.DataFrame(columns=["time", "equity"]),
            starting_balance=starting_balance, ending_balance=starting_balance,
            sum_realized_pnl=0.0, equity_curve_pnl=0.0, reconciles=True,
            reconcile_tolerance=reconcile_tolerance,
        )

    # 1) Compute signals ONCE on the full candles — exactly what run_backtest does.
    sigs = strategy.signals(candles)
    sigs_by_bar = {s.bar_idx: s for s in sigs if 0 <= s.bar_idx < n}

    # 2) Pre-compute ATR if slippage is on (matches backtest's setup)
    if slippage_per_fill_atr_frac > 0.0:
        atr_full = atr_wilder(candles, slippage_atr_period).to_numpy()
    else:
        atr_full = np.zeros(n, dtype=float)

    # 3) Build executor (single-position to match backtest)
    executor = PaperExecutor(
        max_open_positions=1,
        commission_per_trade=commission_per_trade,
        slippage_per_fill_atr_frac=slippage_per_fill_atr_frac,
        time_guard_cfg=time_guard_cfg,
        mode="replay",
    )

    high = candles["high"].to_numpy()
    low = candles["low"].to_numpy()
    close = candles["close"].to_numpy()
    times_pd = candles["time"]

    # tf_seconds for the bar-close-utc projection (forced-flat / no-entry window)
    if n >= 2:
        diffs = times_pd.diff().dt.total_seconds().dropna().to_numpy()
        tf_seconds = int(np.median(diffs))
    else:
        tf_seconds = 0

    def _bar_close_utc(i: int):
        ts = times_pd.iloc[i]
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        return (ts + pd.Timedelta(seconds=tf_seconds)).to_pydatetime()

    balance = float(starting_balance)
    trades: list[ClosedTrade] = []
    equity_rows: list[dict] = []
    skipped_signals = 0

    for i in range(n):
        bar = Bar(
            idx=i, high=float(high[i]), low=float(low[i]),
            close=float(close[i]),
            time_utc=pd.Timestamp(times_pd.iloc[i]).to_pydatetime(),
            atr_value=float(atr_full[i]),
        )
        bar_close = _bar_close_utc(i) if tf_seconds > 0 else None

        # Step 1: SL/TP intrabar — handled inside update_bar
        # (also catches max_hold_bars timing exit if implemented; current
        # PaperExecutor doesn't track time-exit so we mirror it below by
        # manually closing if signal.max_hold_bars is set)
        if executor.has_position(symbol):
            closed = executor.update_bar(symbol, bar, bar_close_utc=bar_close)
            if closed is not None:
                trades.append(closed)
                balance += closed.realized_pnl

        # Step 1c: max_hold_bars time-exit (matches backtest behaviour)
        if executor.has_position(symbol):
            pos = executor.get_position(symbol)
            # Find the signal that opened this position to read max_hold_bars
            opening_sig = sigs_by_bar.get(pos.opened_at_bar_idx)
            if (opening_sig is not None and opening_sig.max_hold_bars > 0
                and i >= pos.opened_at_bar_idx + opening_sig.max_hold_bars):
                closed = executor.close(symbol, float(close[i]),
                                        reason="time", bar=bar)
                if closed is not None:
                    trades.append(closed)
                    balance += closed.realized_pnl

        # Step 2: open new position from a precomputed signal at bar i.
        # Backtest behaviour: silently ignore signals while a position is open.
        sizing_active = (risk_pct is not None and risk_pct > 0
                          and symbol_info is not None)
        should_open = (not executor.has_position(symbol)
                         and i in sigs_by_bar)
        if should_open:
            in_no_entry = (time_guard_cfg is not None
                              and time_guard_cfg.no_entry_minutes_before_close > 0
                              and bar_close is not None
                              and in_no_entry_window(bar_close, time_guard_cfg))
            if in_no_entry:
                skipped_signals += 1
                should_open = False
        if should_open:
            sig = sigs_by_bar[i]
            # Dynamic sizing match-with-backtest path
            if sizing_active:
                from core.position_sizer import calc_lots
                sizing_balance = balance if sizing_uses_running_balance else starting_balance
                res = calc_lots(
                    equity=sizing_balance, risk_pct=float(risk_pct),
                    entry_price=sig.entry_price, stop_price=sig.stop_price,
                    sym=symbol_info,
                )
                if not res.ok:
                    skipped_signals += 1
                    should_open = False
                else:
                    trade_lots = res.lots
            else:
                trade_lots = float(lots)
        if should_open:
            sig = sigs_by_bar[i]
            key = f"{strategy.name}:{symbol}:{tf}:{i}:{sig.bar_idx}"
            executor.open(
                symbol=symbol, direction=sig.direction,
                signal_entry_price=sig.entry_price,
                stop_price=sig.stop_price,
                target_price=sig.target_price,
                lots=trade_lots,
                money_per_unit_price=money_per_unit_price,
                    idempotency_key=key,
                    opened_at_bar_idx=i,
                    opened_at_utc=pd.Timestamp(times_pd.iloc[i]).isoformat(),
                    atr_at_signal_bar=float(atr_full[i]),
                )

        # Step 3: end-of-data close on the LAST bar — apply slippage
        if i == n - 1 and executor.has_position(symbol):
            closed = executor.close(symbol, float(close[i]),
                                    reason="end_of_data", bar=bar)
            if closed is not None:
                trades.append(closed)
                balance += closed.realized_pnl

        # Step 4: mark-to-market
        floating = executor.mark_to_market(symbol, float(close[i]))
        equity_rows.append({"time": times_pd.iloc[i], "equity": balance + floating})

    eq_df = pd.DataFrame(equity_rows)
    sum_realized = float(sum(t.realized_pnl for t in trades))
    eq_pnl = float(eq_df["equity"].iloc[-1] - starting_balance) if len(eq_df) else 0.0
    reconciles = abs(sum_realized - eq_pnl) < reconcile_tolerance

    return ReplayResult(
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
