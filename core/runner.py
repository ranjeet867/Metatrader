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
    skipped_due_to_circuit_breaker: int = 0
    skipped_due_to_position_guard: int = 0
    # When the strategy DETECTED a signal on the last bar, regardless of
    # whether the open succeeded (could have been blocked by guard /
    # parity / sizing / circuit breaker / already-holding). Bar timestamp
    # of the closed bar where the signal printed, ISO-8601 UTC. None
    # means "no signal on this tick". Surfacing this lets the dashboard
    # show `last_signal_at_utc` correctly even when an open is blocked.
    last_signal_bar_utc: Optional[str] = None
    # Always set when this tick processed a closed bar (i.e. strategy
    # was called with a NEW bar, not deduped to "no_new_bar"). Lets the
    # dashboard prove "the runner is alive on this deployment" without
    # waiting for a signal — a stale `last_evaluated_at_utc` is a real
    # red flag, a stale `last_signal_at_utc` may just mean low signal
    # frequency.
    last_evaluated_bar_utc: Optional[str] = None


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
         lots: float = 0.0,
         time_guard_cfg: Optional[TimeGuardCfg] = None,
         atr_period: int = 14,
         compute_atr: bool = False,
         # ----- Phase 27: dynamic lot sizing -----
         risk_pct: float | None = None,
         symbol_info=None,
         account_balance: float | None = None,
         max_lots: float | None = None,
         max_money_risk_usd: float | None = None,
         # ----- Phase 30: production-grade safety hooks -----
         block_opens_reason: str | None = None,
         deployment_id: str = "",
         open_positions_snapshot=None,   # list[OpenPosition] or None
         position_guard_policy: str = "strict",
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
    # Record the last-bar timestamp early so we always surface
    # `last_evaluated_bar_utc` even if a no-entry-window check returns
    # before signals are computed.
    try:
        out.last_evaluated_bar_utc = pd.Timestamp(
            view["time"].iloc[last_idx]
        ).tz_convert("UTC").isoformat() if pd.Timestamp(
            view["time"].iloc[last_idx]
        ).tz is not None else pd.Timestamp(
            view["time"].iloc[last_idx]
        ).tz_localize("UTC").isoformat()
    except Exception:
        pass

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

    # --- 2b. Circuit breaker — host pre-evaluated, refuse opens entirely ---
    # The Operations / live runner calls circuit_breaker.evaluate() and
    # passes a non-None reason here when state != OK. We still call
    # update_bar above (existing positions can ride to SL/TP) but skip
    # any new opens.
    if block_opens_reason:
        out.skipped_due_to_circuit_breaker += 1
        out.errors.append(
            f"circuit_breaker BLOCK: {block_opens_reason}"
        )
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

    # Signal DETECTED — record the bar timestamp regardless of whether
    # the open succeeds below. Position-guard, sizing-rejection, and
    # idempotency-collision all leave `opens` empty but the strategy
    # genuinely fired — the dashboard should reflect that.
    out.last_signal_bar_utc = out.last_evaluated_bar_utc

    # --- 5. open via idempotency-keyed call ---
    sizing_active = (risk_pct is not None and risk_pct > 0
                       and symbol_info is not None
                       and account_balance is not None)
    for sig in new_sigs:
        # Compute lots: dynamic if sizing config supplied, else fixed `lots`.
        if sizing_active:
            from core.position_sizer import calc_lots
            res = calc_lots(
                equity=float(account_balance), risk_pct=float(risk_pct),
                entry_price=sig.entry_price, stop_price=sig.stop_price,
                sym=symbol_info,
                max_lots=max_lots,
                max_money_risk_usd=max_money_risk_usd,
            )
            if not res.ok:
                out.errors.append(f"sizing rejected: {res.reason}")
                continue
            trade_lots = res.lots
        else:
            trade_lots = float(lots)

        # Position-collision guard — reject opens that conflict with
        # another deployment's existing position on the same symbol.
        # Caller passes the cross-deployment positions snapshot in
        # `open_positions_snapshot`; absent → guard not enforced.
        if open_positions_snapshot is not None and deployment_id:
            from core.position_guard import check_open as _guard_check
            decision = _guard_check(
                new_symbol=symbol,
                new_side=sig.direction,
                new_deployment_id=deployment_id,
                open_positions=open_positions_snapshot,
                policy=position_guard_policy,
            )
            if decision.decision in ("BLOCK", "STACK"):
                out.skipped_due_to_position_guard += 1
                out.errors.append(
                    f"position_guard {decision.decision}: {decision.reason}"
                )
                continue

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
                lots=trade_lots,
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
