"""
runner.py — atomic per-bar tick. The single function called by replay,
paper_loop, and (eventually) live_loop. Keeping the per-bar state machine
in ONE place is what makes replay-parity (INVARIANT-2) achievable.

Order of operations per tick:

  1. last_bar = view.iloc[-1]
  2. update_bar on the executor for the symbol's open position
     (this handles SL/TP intrabar AND time-guard forced-flats).
  3. If no_entry_window applies to last_bar.close → skip the signal pass.
  4. Otherwise: compute strategy.signals(view) and filter to bar_idx ==
     len(view) - 1 (the LAST bar of the view — live discipline).
  5. For each new signal, build idempotency_key = "<strat>:<sym>:<tf>:<bar_t>"
     and call executor.open. Catch IdempotencyCollision (== already opened
     this bar — no-op) but re-raise other errors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd

from core.backtest import ClosedTrade
from core.indicators import atr_wilder
from core.paper_executor import (
    Bar,
    IdempotencyCollision,
    PaperExecutor,
    PaperPosition,
    SymbolAlreadyOpen,
)
from core.strategy import Signal, Strategy
from core.time_guards import TimeGuardCfg, in_no_entry_window


@dataclass
class TickResult:
    """Effects of a single tick. Used by replay/paper to drive equity + journal."""
    opens: list[PaperPosition] = field(default_factory=list)
    closes: list[ClosedTrade] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_due_to_no_entry_window: int = 0


def _tf_seconds_from_view(view: pd.DataFrame) -> int:
    """Median bar interval in seconds. 0 when fewer than 2 bars."""
    if len(view) < 2:
        return 0
    diffs = view["time"].diff().dt.total_seconds().dropna().to_numpy()
    if len(diffs) == 0:
        return 0
    import numpy as np
    return int(np.median(diffs))


def _bar_close_utc(view: pd.DataFrame, idx: int, tf_seconds: int) -> datetime:
    ts = view["time"].iloc[idx]
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    return (ts + pd.Timedelta(seconds=tf_seconds)).to_pydatetime()


def _atr_at_bar(view: pd.DataFrame, idx: int, period: int = 14) -> float:
    """ATR(period) value at bar idx. 0 if not enough bars."""
    if len(view) < 2:
        return 0.0
    series = atr_wilder(view, period)
    return float(series.iloc[idx])


def make_idempotency_key(strategy_name: str, symbol: str, tf: str,
                         bar_time: pd.Timestamp) -> str:
    """Deterministic key — same view + same signal => same key."""
    iso = pd.Timestamp(bar_time).tz_convert("UTC").isoformat() \
          if pd.Timestamp(bar_time).tz is not None \
          else pd.Timestamp(bar_time).tz_localize("UTC").isoformat()
    return f"{strategy_name}:{symbol}:{tf}:{iso}"


def tick(view: pd.DataFrame,
         executor: PaperExecutor,
         strategy: Strategy,
         *,
         symbol: str,
         tf: str,
         money_per_unit_price: float,
         lots: float,
         time_guard_cfg: Optional[TimeGuardCfg] = None,
         atr_period: int = 14,
         compute_atr: bool = False,
         ) -> TickResult:
    """One atomic processing step over `view` (a 1+-bar window of candles).

    The runner only ACTS on the LAST bar of `view`:
      - SL/TP/time-guard exits via executor.update_bar
      - One signal-fire eligible only if signal.bar_idx == len(view) - 1

    Args:
        view: candle DataFrame; columns time/open/high/low/close/volume.
        executor: a PaperExecutor (or compatible).
        strategy: the strategy whose .signals(view) we run.
        symbol: broker symbol — used in idempotency key + asset_class.
        tf: timeframe label — used in idempotency key.
        money_per_unit_price: $ per 1.0 price unit per 1 lot.
        lots: fixed-lot size for any new opens.
        time_guard_cfg: if provided, no-entry-window + forced-flat are active.
        atr_period: lookback for slippage ATR (only used when compute_atr=True).
        compute_atr: when True, compute ATR at the last bar and pass it to
                     executor.open (for slippage). Default False to keep ticks
                     fast in paper-trading where slippage is usually 0.
    """
    out = TickResult()
    n = len(view)
    if n == 0:
        return out

    last_idx = n - 1

    # --- 1. update_bar on existing position ---
    last_row = view.iloc[last_idx]
    atr_val = _atr_at_bar(view, last_idx, atr_period) if compute_atr else 0.0
    bar = Bar(
        idx=last_idx,
        high=float(last_row["high"]),
        low=float(last_row["low"]),
        close=float(last_row["close"]),
        time_utc=pd.Timestamp(last_row["time"]).to_pydatetime(),
        atr_value=atr_val,
    )
    tf_seconds = _tf_seconds_from_view(view)
    bar_close_utc = (_bar_close_utc(view, last_idx, tf_seconds)
                     if tf_seconds > 0 else None)

    if executor.has_position(symbol):
        try:
            closed = executor.update_bar(symbol, bar,
                                         bar_close_utc=bar_close_utc)
            if closed is not None:
                out.closes.append(closed)
        except Exception as e:    # pragma: no cover — defensive
            out.errors.append(f"update_bar: {type(e).__name__}: {e}")

    # --- 2. no-entry window check (skip signal scan) ---
    if (time_guard_cfg is not None
        and time_guard_cfg.no_entry_minutes_before_close > 0
        and bar_close_utc is not None
        and in_no_entry_window(bar_close_utc, time_guard_cfg)):
        out.skipped_due_to_no_entry_window += 1
        return out

    # --- 3 + 4. compute signals, filter to last bar ---
    try:
        sigs = strategy.signals(view)
    except Exception as e:    # pragma: no cover — defensive
        out.errors.append(f"strategy.signals: {type(e).__name__}: {e}")
        return out

    new_sigs = [s for s in sigs if s.bar_idx == last_idx]
    if not new_sigs:
        return out

    # --- 5. open via idempotency-keyed call ---
    for sig in new_sigs:
        key = make_idempotency_key(
            strategy.name, symbol, tf, view["time"].iloc[last_idx]
        )
        try:
            pos = executor.open(
                symbol=symbol,
                direction=sig.direction,
                signal_entry_price=sig.entry_price,
                stop_price=sig.stop_price,
                target_price=sig.target_price,
                lots=lots,
                money_per_unit_price=money_per_unit_price,
                idempotency_key=key,
                opened_at_bar_idx=last_idx,
                opened_at_utc=pd.Timestamp(view["time"].iloc[last_idx]).isoformat(),
                atr_at_signal_bar=atr_val,
            )
            out.opens.append(pos)
        except IdempotencyCollision:
            # Same view processed twice — first call already opened, no-op
            pass
        except SymbolAlreadyOpen:
            # Backtest behaviour: a second signal while a position is open
            # is silently ignored. Replay-parity requires the same.
            pass
        except Exception as e:    # pragma: no cover — defensive
            out.errors.append(f"open: {type(e).__name__}: {e}")

    return out
