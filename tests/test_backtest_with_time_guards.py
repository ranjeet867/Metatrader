"""
test_backtest_with_time_guards.py — INVARIANT-8 backtest behaviour.

When the time-guard params are enabled, the backtest must:
  1. Force-close at Friday close-window (weekend_flat) regardless of class.
  2. Force-close at weekday close-window for stocks/indices (daily_close_flat).
  3. NOT force-close FX/metals at weekday close (only weekend).
  4. Skip new opens when in_no_entry_window.
  5. Still satisfy reconciliation invariant in BOTH cases (with/without).

Tests use synthetic candles whose timestamps walk through close windows so
we can assert exact close_reasons.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.backtest import run_backtest
from core.strategy import Signal


def _hourly_candles_two_days(start: str = "2026-05-04T00:00:00Z") -> pd.DataFrame:
    """48 H1 bars starting at midnight UTC. Day 1 is Monday 2026-05-04;
    day 2 is Tuesday 2026-05-05. Prices form a simple ramp so a long is
    always floating-positive when held."""
    n = 48
    times = pd.date_range(start, periods=n, freq="h", tz="UTC")
    closes = 100.0 + np.arange(n, dtype=float) * 0.1
    df = pd.DataFrame({
        "time": times,
        "open":  closes - 0.05,
        "high":  closes + 0.05,
        "low":   closes - 0.05,
        "close": closes,
        "volume": np.full(n, 1000.0),
    })
    return df


def _hourly_candles_friday() -> pd.DataFrame:
    """48 H1 bars starting Thursday 2026-05-07 00:00 UTC, covering Thu+Fri."""
    return _hourly_candles_two_days("2026-05-07T00:00:00Z")


def _open_long_signal(bar_idx: int, candles: pd.DataFrame, target_dist=10.0,
                      stop_dist=5.0) -> Signal:
    """Build a LONG signal at bar_idx with a stop and target neither of
    which will hit on our flat-priced ramp (so SL/TP never fire and we
    can isolate the time-guard exits)."""
    entry = float(candles["close"].iloc[bar_idx])
    return Signal(
        bar_idx=bar_idx, direction="LONG",
        entry_price=entry,
        stop_price=entry - stop_dist,
        target_price=entry + target_dist,
        reason="test",
    )


class TestDailyFlatOnIndex:
    """Index symbol (US100.cash): daily_flat must close at weekday 19:55-ish."""

    def test_daily_flat_closes_long_at_close_window(self):
        df = _hourly_candles_two_days()       # Mon + Tue, hourly
        # Open LONG at Monday 09:00 UTC (bar idx 9). Position would otherwise
        # ride all the way through to EOD on Tuesday 23:00.
        sig = _open_long_signal(9, df)
        result = run_backtest(
            df, [sig],
            starting_balance=100_000, lots=1.0, money_per_unit_price=1.0,
            symbol="US100.cash",
            enforce_daily_flat=True,
            daily_close_flat_classes=("stock", "index"),
        )
        assert result.reconciles, f"div={result.reconcile_divergence}"
        # Exactly one trade closed by daily_close_flat
        flat_trades = [t for t in result.trades if t.close_reason == "daily_close_flat"]
        assert len(flat_trades) == 1
        # The exit_bar should correspond to the bar whose close is at 20:00 UTC
        # (the H1 bar at 19:00 closes at 20:00). On Monday that is bar 19.
        assert flat_trades[0].exit_bar_idx == 19
        # Mon 19:00 close
        exit_time = df["time"].iloc[19]
        assert exit_time.hour == 19

    def test_daily_flat_off_holds_position_to_eod(self):
        df = _hourly_candles_two_days()
        sig = _open_long_signal(9, df)
        result = run_backtest(
            df, [sig],
            starting_balance=100_000, lots=1.0, money_per_unit_price=1.0,
            symbol="US100.cash",
            enforce_daily_flat=False,
        )
        assert result.reconciles
        # No daily_close_flat trade
        assert all(t.close_reason != "daily_close_flat" for t in result.trades)

    def test_with_and_without_flat_differ(self):
        df = _hourly_candles_two_days()
        sig = _open_long_signal(9, df)
        with_flat = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0, symbol="US100.cash",
            enforce_daily_flat=True,
        )
        no_flat = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0, symbol="US100.cash",
            enforce_daily_flat=False,
        )
        # Both must reconcile
        assert with_flat.reconciles and no_flat.reconciles
        # PnL differs (close at 19:00 close vs final bar close)
        assert abs(with_flat.sum_realized_pnl - no_flat.sum_realized_pnl) > 0.5


class TestDailyFlatOnFx:
    """FX (EURUSD) is exempt from daily_flat — only weekend_flat affects it."""

    def test_daily_flat_does_not_close_fx(self):
        df = _hourly_candles_two_days()
        sig = _open_long_signal(9, df)
        result = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0,
            symbol="EURUSD",
            enforce_daily_flat=True,
            daily_close_flat_classes=("stock", "index"),
        )
        assert result.reconciles
        assert all(t.close_reason != "daily_close_flat" for t in result.trades)


class TestWeekendFlat:
    """Friday close-window: ALL positions close regardless of class."""

    def test_weekend_flat_closes_long_on_friday(self):
        df = _hourly_candles_friday()        # Thu + Fri
        sig = _open_long_signal(0 + 24 + 9, df)   # bar at Friday 09:00
        result = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0,
            symbol="EURUSD",     # FX, exempt from daily_flat
            enforce_weekend_flat=True,
            enforce_daily_flat=False,
        )
        assert result.reconciles
        flat = [t for t in result.trades if t.close_reason == "weekend_flat"]
        assert len(flat) == 1
        # Closed at Friday 19:00 H1 bar (closes 20:00)
        exit_t = df["time"].iloc[flat[0].exit_bar_idx]
        assert exit_t.weekday() == 4 and exit_t.hour == 19

    def test_weekend_flat_takes_precedence_over_daily(self):
        """When both fire on a Friday, the close_reason MUST be weekend_flat
        (not daily_close_flat)."""
        df = _hourly_candles_friday()
        sig = _open_long_signal(24 + 9, df)
        result = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0,
            symbol="US100.cash",     # index — daily_flat would also fire
            enforce_weekend_flat=True,
            enforce_daily_flat=True,
        )
        assert result.reconciles
        flat = [t for t in result.trades if t.close_reason == "weekend_flat"]
        assert len(flat) == 1


class TestNoEntryWindow:
    """Signals that would open inside the no-entry window are skipped."""

    def test_signal_in_no_entry_window_skipped(self):
        df = _hourly_candles_two_days()
        # Place a signal at H1 bar 19:00 (Mon) — its close is 20:00, which
        # IS in [19:30, 20:00) no-entry window
        sig = _open_long_signal(19, df)
        result = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0,
            symbol="US100.cash",
            no_entry_minutes_before_close=30,
        )
        assert result.reconciles
        assert result.skipped_signals == 1
        assert result.n_trades == 0

    def test_signal_outside_window_still_opens(self):
        df = _hourly_candles_two_days()
        # Bar 12 is Monday 12:00 → bar close at 13:00 — well outside no-entry
        sig = _open_long_signal(12, df)
        result = run_backtest(
            df, [sig], starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0,
            symbol="US100.cash",
            no_entry_minutes_before_close=30,
        )
        assert result.reconciles
        assert result.skipped_signals == 0
        assert result.n_trades == 1


class TestReconciliationUnchanged:
    """The reconciliation invariant must hold whether or not flats are on."""

    def test_reconciles_with_all_guards_on(self):
        df = _hourly_candles_friday()
        sigs = [_open_long_signal(idx, df)
                for idx in range(0, 48, 7) if idx + 1 < len(df)]
        # validate signal geometry — drop any malformed
        sigs = [s for s in sigs if s.stop_price < s.entry_price < s.target_price]
        result = run_backtest(
            df, sigs, starting_balance=100_000, lots=1.0,
            money_per_unit_price=1.0,
            symbol="US100.cash",
            enforce_weekend_flat=True,
            enforce_daily_flat=True,
            no_entry_minutes_before_close=30,
        )
        assert result.reconciles, f"div={result.reconcile_divergence}"
