"""
paper_executor.py — in-memory single-position paper trading engine.

INVARIANT-3: imports the PnL formula + slippage helpers from core.backtest.
No duplication of money math. Numerically identical results to run_backtest
on identical inputs (verified by tests/test_replay_parity.py).

INVARIANT-8: update_bar() force-closes positions when the bar's effective
close time falls in a weekend_flat or daily_close_flat window.

Idempotency: every open() requires an `idempotency_key`. Re-opening with the
same key on the same executor raises IdempotencyCollision — this guards
against the bridge-timeout retry that would otherwise cause duplicate fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.backtest import (
    ClosedTrade,
    apply_entry_slip,
    apply_exit_slip,
    compute_initial_dollar_risk,
    compute_realized_pnl,
)
from core.time_guards import (
    TimeGuardCfg,
    needs_daily_flat,
    needs_weekend_flat,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class PaperExecutorError(Exception):
    """Base for paper-executor refusals."""


class IdempotencyCollision(PaperExecutorError):
    """An open() with a key that has been seen before."""


class MaxOpenPositionsExceeded(PaperExecutorError):
    """The executor's max_open_positions cap would be exceeded."""


class SymbolAlreadyOpen(PaperExecutorError):
    """We already hold a position on this symbol (single-symbol-position rule)."""


class BadSignalGeometry(PaperExecutorError):
    """LONG with stop above entry; SHORT with stop below entry; etc."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaperPosition:
    """An open paper position. Frozen + hashable so callers can use as dict key."""
    symbol: str
    direction: str                  # "LONG" | "SHORT"
    signal_entry_price: float       # pre-slippage signal price
    actual_entry_price: float       # post-slippage actual fill
    stop_price: float
    target_price: float
    lots: float
    money_per_unit_price: float
    entry_slip_abs: float
    opened_at_bar_idx: int
    opened_at_utc: str              # ISO8601
    idempotency_key: str
    initial_dollar_risk: float


@dataclass(frozen=True)
class Bar:
    """The minimal bar view PaperExecutor needs. (We don't take a pandas
    Series so the executor is decoupled from pandas at the interface.)"""
    idx: int
    high: float
    low: float
    close: float
    time_utc: datetime              # bar's OPEN time (matches the candles' time col)
    atr_value: float = 0.0          # ATR at this bar — used for slippage if > 0


# ---------------------------------------------------------------------------
# PaperExecutor
# ---------------------------------------------------------------------------

class PaperExecutor:
    """In-memory paper trading engine. Single position per symbol, capped
    by max_open_positions across symbols.

    Args:
        max_open_positions: across-symbols cap.
        commission_per_trade: in account currency, applied at close.
        slippage_per_fill_atr_frac: see core.backtest.run_backtest for semantics.
        time_guard_cfg: optional. If provided, update_bar() force-flats
            positions whose effective close time falls in a weekend or
            daily-close window. The executor defers the bar's effective
            close-time computation to the caller — see Bar.time_utc + tf_seconds
            via the runner.
        mode: 'paper' | 'live' — purely for trade-record annotation.
    """

    def __init__(self, *,
                 max_open_positions: int = 3,
                 commission_per_trade: float = 0.0,
                 slippage_per_fill_atr_frac: float = 0.0,
                 time_guard_cfg: Optional[TimeGuardCfg] = None,
                 mode: str = "paper") -> None:
        self.max_open_positions = int(max_open_positions)
        self.commission_per_trade = float(commission_per_trade)
        self.slippage_per_fill_atr_frac = float(slippage_per_fill_atr_frac)
        self.time_guard_cfg = time_guard_cfg
        self.mode = mode

        self._positions: dict[str, PaperPosition] = {}   # symbol -> position
        self._seen_keys: set[str] = set()
        self._closed_trades: list[ClosedTrade] = []

    # --- introspection -----------------------------------------------------

    @property
    def n_open(self) -> int:
        return len(self._positions)

    @property
    def closed_trades(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._closed_trades)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._positions

    def get_position(self, symbol: str) -> Optional[PaperPosition]:
        return self._positions.get(symbol)

    # --- main API ----------------------------------------------------------

    def open(self, *, symbol: str, direction: str,
             signal_entry_price: float, stop_price: float, target_price: float,
             lots: float, money_per_unit_price: float,
             idempotency_key: str,
             opened_at_bar_idx: int, opened_at_utc: str,
             atr_at_signal_bar: float = 0.0) -> PaperPosition:
        """Open a new position. INVARIANT-3 slippage applied via apply_entry_slip
        from core.backtest. Refuses on duplicate idempotency_key, max_open
        exceeded, symbol already held, or bad geometry."""
        if idempotency_key in self._seen_keys:
            raise IdempotencyCollision(
                f"key {idempotency_key!r} already used in this session"
            )
        if symbol in self._positions:
            raise SymbolAlreadyOpen(f"already have a position on {symbol!r}")
        if len(self._positions) >= self.max_open_positions:
            raise MaxOpenPositionsExceeded(
                f"max_open_positions={self.max_open_positions} reached"
            )
        if direction not in ("LONG", "SHORT"):
            raise BadSignalGeometry(f"direction must be LONG/SHORT; got {direction!r}")
        # Geometry sanity (matches run_backtest's validation)
        if direction == "LONG":
            if not (stop_price < signal_entry_price < target_price):
                raise BadSignalGeometry(
                    f"bad LONG: stop={stop_price} entry={signal_entry_price} "
                    f"target={target_price}"
                )
        else:
            if not (target_price < signal_entry_price < stop_price):
                raise BadSignalGeometry(
                    f"bad SHORT: target={target_price} entry={signal_entry_price} "
                    f"stop={stop_price}"
                )
        if lots <= 0:
            raise BadSignalGeometry(f"lots must be > 0; got {lots}")

        entry_slip_abs = atr_at_signal_bar * self.slippage_per_fill_atr_frac
        actual_entry = apply_entry_slip(direction, signal_entry_price, entry_slip_abs)
        init_risk = compute_initial_dollar_risk(
            direction, signal_entry_price, stop_price, lots, money_per_unit_price,
        )

        pos = PaperPosition(
            symbol=symbol,
            direction=direction,
            signal_entry_price=signal_entry_price,
            actual_entry_price=actual_entry,
            stop_price=stop_price,
            target_price=target_price,
            lots=lots,
            money_per_unit_price=money_per_unit_price,
            entry_slip_abs=entry_slip_abs,
            opened_at_bar_idx=opened_at_bar_idx,
            opened_at_utc=opened_at_utc,
            idempotency_key=idempotency_key,
            initial_dollar_risk=init_risk,
        )
        self._positions[symbol] = pos
        self._seen_keys.add(idempotency_key)
        return pos

    def update_bar(self, symbol: str, bar: Bar,
                   bar_close_utc: Optional[datetime] = None
                   ) -> Optional[ClosedTrade]:
        """Process a new bar for `symbol`.

        Resolution order (matches core.backtest):
          1. SL/TP intrabar check — pessimistic (SL first if both)
          2. INVARIANT-8 forced-flats (only if time_guard_cfg AND
             bar_close_utc are set):
                weekend_flat takes precedence over daily_close_flat.
                Closes at bar.close.

        Returns a ClosedTrade if the bar closed the position, else None.
        """
        pos = self._positions.get(symbol)
        if pos is None:
            return None

        # 1. SL / TP
        if pos.direction == "LONG":
            hit_stop = bar.low <= pos.stop_price
            hit_target = bar.high >= pos.target_price
        else:
            hit_stop = bar.high >= pos.stop_price
            hit_target = bar.low <= pos.target_price

        if hit_stop:
            return self._close_at_level(symbol, pos.stop_price, "stop", bar)
        if hit_target:
            return self._close_at_level(symbol, pos.target_price, "target", bar)

        # 2. Forced-flat. Both daily and weekend flats are now gated by
        # explicit flags on the cfg, mirroring run_backtest's API. Pre-
        # fix daily-flat fired whenever `daily_close_flat_classes` was
        # non-empty — diverging from backtest when callers set weekend
        # flat WITHOUT intending daily flat. Now they're independent.
        if self.time_guard_cfg is not None and bar_close_utc is not None:
            if (self.time_guard_cfg.weekend_flat_all
                and needs_weekend_flat(bar_close_utc, self.time_guard_cfg)):
                return self._close_at_level(symbol, bar.close,
                                              "weekend_flat", bar)
            if (getattr(self.time_guard_cfg, "enforce_daily_flat", True)
                and needs_daily_flat(symbol, bar_close_utc, self.time_guard_cfg)):
                return self._close_at_level(symbol, bar.close,
                                              "daily_close_flat", bar)

        return None

    def close(self, symbol: str, mark_price: float, reason: str,
              bar: Optional[Bar] = None) -> Optional[ClosedTrade]:
        """Force-close at an arbitrary mark price. `reason` is recorded as
        close_reason on the trade record. Returns None if no position open.

        The slippage on this exit is taken from `bar.atr_value` if `bar` is
        given; otherwise no slippage is applied (the mark_price IS the fill).
        """
        if symbol not in self._positions:
            return None
        return self._close_at_level(symbol, mark_price, reason, bar)

    def mark_to_market(self, symbol: str, mark_price: float) -> float:
        """Floating PnL of the open position at `mark_price`. 0 if no position.

        Uses the SAME formula as compute_realized_pnl with commission=0 (we
        don't pre-charge commission on open positions; it lands on close)."""
        pos = self._positions.get(symbol)
        if pos is None:
            return 0.0
        return compute_realized_pnl(
            direction=pos.direction,
            actual_entry=pos.actual_entry_price,
            actual_exit=mark_price,
            lots=pos.lots,
            money_per_unit_price=pos.money_per_unit_price,
            commission_per_trade=0.0,
        )

    # --- private -----------------------------------------------------------

    def _close_at_level(self, symbol: str, level_price: float, reason: str,
                        bar: Optional[Bar]) -> ClosedTrade:
        pos = self._positions.pop(symbol)
        atr = bar.atr_value if bar is not None else 0.0
        exit_slip_abs = atr * self.slippage_per_fill_atr_frac
        actual_exit = apply_exit_slip(pos.direction, level_price, exit_slip_abs)

        pnl = compute_realized_pnl(
            direction=pos.direction,
            actual_entry=pos.actual_entry_price,
            actual_exit=actual_exit,
            lots=pos.lots,
            money_per_unit_price=pos.money_per_unit_price,
            commission_per_trade=self.commission_per_trade,
        )
        r_multiple = pnl / pos.initial_dollar_risk if pos.initial_dollar_risk > 0 else 0.0

        trade = ClosedTrade(
            direction=pos.direction,
            entry_bar_idx=pos.opened_at_bar_idx,
            exit_bar_idx=bar.idx if bar is not None else pos.opened_at_bar_idx,
            entry_price=pos.actual_entry_price,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            exit_price=actual_exit,
            lots=pos.lots,
            money_per_unit_price=pos.money_per_unit_price,
            realized_pnl=pnl,
            r_multiple=r_multiple,
            initial_dollar_risk=pos.initial_dollar_risk,
            close_reason=reason,
            signal_entry_price=pos.signal_entry_price,
            entry_slip=pos.entry_slip_abs,
            exit_slip=exit_slip_abs,
        )
        self._closed_trades.append(trade)
        return trade
