"""
test_strategies.py — verify each strategy fires the expected signals on
hand-crafted synthetic data.

These tests focus on STRUCTURAL invariants that any correct implementation
must satisfy:
  - flat data → no signals
  - signal geometry valid (LONG: stop<entry<target; SHORT: mirror)
  - same input → identical signals (idempotency)
  - long_only mode emits zero SHORTs
  - end-to-end via run_backtest reconciles
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.backtest import run_backtest
from strategies.ema_pullback import EmaPullback, EmaPullbackParams
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
from strategies.bbands_meanrev import BBandsMeanRev, BBandsMeanRevParams
from strategies.ibs import Ibs, IbsParams
from strategies.inside_bar import InsideBar, InsideBarParams
from strategies.orb import Orb, OrbParams
from strategies.overnight_drift import OvernightDrift, OvernightDriftParams
from tests.fixtures.synthetic import constant, linear_ramp, sawtooth, step_function


ALL_STRATEGIES = [
    ("ema_pullback",       lambda: EmaPullback(EmaPullbackParams())),
    ("donchian_breakout",  lambda: DonchianBreakout(DonchianBreakoutParams(period=10))),
    ("rsi_meanrev",        lambda: RsiMeanRev(RsiMeanRevParams())),
    ("bbands_meanrev",     lambda: BBandsMeanRev(BBandsMeanRevParams())),
    ("ibs",                lambda: Ibs(IbsParams())),
    ("overnight_drift",    lambda: OvernightDrift(OvernightDriftParams())),
    ("orb",                lambda: Orb(OrbParams())),
    ("inside_bar",         lambda: InsideBar(InsideBarParams())),
]


def _m15_session_candles(n_sessions: int = 3, breakout_above: bool = True,
                          start_date: str = "2024-01-02") -> pd.DataFrame:
    """Build M15 candles spanning n_sessions US sessions (13:30 → 19:45 UTC).

    Session structure:
      bar 0..3 (13:30, 13:45, 14:00, 14:15) — OR forms.
      bar 4 (14:30)                        — breakout bar.
      bars 5..25 (14:45..19:45)            — drift toward target.
      bar 26 (20:00)                       — session close (NOT included).

    With breakout_above=True the breakout bar's close is well above OR_high.
    With breakout_above=False the breakout bar's close is well below OR_low.
    """
    bars_per_session = 26   # 13:30 .. 19:45 inclusive
    rows = []
    base_price = 100.0
    for d in range(n_sessions):
        date = pd.Timestamp(start_date, tz="UTC") + pd.Timedelta(days=d)
        for i in range(bars_per_session):
            t = date + pd.Timedelta(hours=13, minutes=30) + pd.Timedelta(minutes=15 * i)
            # Default close = base_price + small noise per bar within the OR window
            if i < 4:
                close = base_price + (i * 0.05)   # drift slightly within OR
            elif i == 4:
                # Breakout bar: cleanly above (or below) OR
                close = base_price + (1.0 if breakout_above else -1.0)
            else:
                # Drift toward the breakout direction (give target a chance)
                close = base_price + ((i - 3) * (0.05 if breakout_above else -0.05))
            rng = 0.10
            row = {
                "time": t,
                "open": close,
                "high": close + rng,
                "low": close - rng,
                "close": close,
                "volume": 1000.0,
            }
            rows.append(row)
        base_price += 0.5  # tiny day-on-day drift so sessions aren't identical
    return pd.DataFrame(rows)


# ===========================================================================
# Universal invariants — must hold for EVERY strategy
# ===========================================================================
class TestUniversalInvariants:

    def test_flat_data_yields_no_signals(self):
        """Constant prices → no strategy should fire."""
        df = constant(price=100, n_bars=300)
        for name, build in ALL_STRATEGIES:
            sigs = build().signals(df)
            assert sigs == [], f"{name} fired on flat data: {sigs}"

    def test_signal_geometry_correct(self):
        """Every signal must have stop/entry/target on correct sides."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=20, down_bars=20, n_cycles=10)
        for name, build in ALL_STRATEGIES:
            for s in build().signals(df):
                if s.direction == "LONG":
                    assert s.stop_price < s.entry_price < s.target_price, \
                        f"{name} bad LONG geometry: {s}"
                else:
                    assert s.target_price < s.entry_price < s.stop_price, \
                        f"{name} bad SHORT geometry: {s}"

    def test_signals_idempotent(self):
        """Same input → identical output, every time."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)
        for name, build in ALL_STRATEGIES:
            a = build().signals(df)
            b = build().signals(df)
            assert len(a) == len(b), f"{name} not idempotent (lengths differ)"
            for sa, sb in zip(a, b):
                assert sa == sb, f"{name} not idempotent: {sa} vs {sb}"

    def test_signals_in_time_order(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)
        for name, build in ALL_STRATEGIES:
            sigs = build().signals(df)
            for i in range(1, len(sigs)):
                assert sigs[i].bar_idx > sigs[i - 1].bar_idx, \
                    f"{name} signals out of order at idx {i}"


# ===========================================================================
# End-to-end RECONCILIATION must hold for every strategy
# ===========================================================================
class TestE2EReconciliation:
    def test_all_strategies_reconcile_on_sawtooth(self):
        """Each strategy's signals fed to run_backtest must reconcile."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=20, down_bars=20, n_cycles=15)
        for name, build in ALL_STRATEGIES:
            sigs = build().signals(df)
            r = run_backtest(df, sigs, starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0)
            assert r.reconciles, \
                f"{name} did not reconcile: " \
                f"sum_pnl={r.sum_realized_pnl:.4f}  " \
                f"eq_pnl={r.equity_curve_pnl:.4f}"


# ===========================================================================
# Strategy-specific shape tests (light, just enough to catch obvious bugs)
# ===========================================================================
class TestDonchianFiresOnStep:
    def test_donchian_fires_after_step_up(self):
        """Step from 100 to 110 → eventually a LONG breakout signal."""
        df = step_function(low_price=100, high_price=110,
                            bars_at_low=50, bars_at_high=50)
        sigs = DonchianBreakout(DonchianBreakoutParams(period=10)).signals(df)
        long_signals = [s for s in sigs if s.direction == "LONG"]
        assert len(long_signals) >= 1, \
            f"Donchian should fire on step-up; got {sigs}"
        assert long_signals[0].bar_idx >= 50


class TestLongOnlyMode:
    """Each strategy must respect long_only=True."""

    def test_long_only_emits_no_shorts_donchian(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)
        sigs = DonchianBreakout(
            DonchianBreakoutParams(period=10, long_only=True)
        ).signals(df)
        assert all(s.direction == "LONG" for s in sigs)

    def test_long_only_emits_no_shorts_rsi(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)
        sigs = RsiMeanRev(RsiMeanRevParams(long_only=True)).signals(df)
        assert all(s.direction == "LONG" for s in sigs)

    def test_long_only_emits_no_shorts_bbands(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=10, down_bars=10, n_cycles=15)
        sigs = BBandsMeanRev(BBandsMeanRevParams(long_only=True)).signals(df)
        assert all(s.direction == "LONG" for s in sigs)


# ===========================================================================
# IBS strategy — specific tests
# ===========================================================================
class TestIbsStrategy:
    def test_only_emits_long(self):
        """IBS as implemented is long-only by design."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=10, down_bars=10, n_cycles=15)
        sigs = Ibs(IbsParams()).signals(df)
        assert all(s.direction == "LONG" for s in sigs)

    def test_fires_on_close_at_low_then_recovery(self):
        """Construct candles where bar i-1 closes near its low (low IBS) and
        bar i closes above bar i-1's close. IBS LONG should fire at bar i."""
        n = 50
        times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
        # Rising trend so prior close is comparable
        closes = np.linspace(100, 110, n)
        highs = closes + 1.0
        lows = closes - 1.0
        # Bar 30: prior bar (29) closes near LOW of its bar; today closes UP from prior.
        # Prior bar IBS = (close-low)/(high-low) = (close-(close-1))/(2) = 0.5 normally.
        # Push it under 0.20: set bar 29 close to bar 29 low + 0.1 of range.
        rng_29 = 2.0
        lows[29] = closes[29] - 1.0
        highs[29] = closes[29] + 1.0
        # Force closes[29] near low: replace it
        # IBS = (close-low)/(high-low) = 0.1/2 = 0.05 < 0.20 ✓
        lows[29] = closes[29] - 0.1
        highs[29] = closes[29] + 1.9
        # And bar 30 closes above bar 29 close
        # closes[30] = closes[29] + 0.5 > closes[29]: linspace already gives that
        df = pd.DataFrame({
            "time": times, "open": closes, "high": highs, "low": lows,
            "close": closes, "volume": np.full(n, 1000.0),
        })
        sigs = Ibs(IbsParams(atr_period=14, max_hold=5)).signals(df)
        assert len(sigs) >= 1
        assert any(s.bar_idx == 30 and s.direction == "LONG" for s in sigs)

    def test_max_hold_bars_set_on_signal(self):
        """Every IBS signal must carry max_hold_bars > 0."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)
        sigs = Ibs(IbsParams(max_hold=4)).signals(df)
        for s in sigs:
            assert s.max_hold_bars == 4, f"expected max_hold=4, got {s.max_hold_bars}"

    def test_zero_range_bars_no_div_by_zero(self):
        """Constant prices yield zero-range bars — must not crash."""
        df = constant(price=100, n_bars=300)
        sigs = Ibs(IbsParams()).signals(df)
        assert sigs == []


# ===========================================================================
# OvernightDrift strategy — specific tests
# ===========================================================================
class TestOvernightDriftStrategy:
    def test_only_emits_long(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=5, down_bars=5, n_cycles=20)
        sigs = OvernightDrift(OvernightDriftParams()).signals(df)
        assert all(s.direction == "LONG" for s in sigs)

    def test_one_signal_per_eligible_bar(self):
        """OvernightDrift emits a signal at every bar (after warm-up, before last)."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=5, down_bars=5, n_cycles=20)
        params = OvernightDriftParams(atr_period=14)
        sigs = OvernightDrift(params).signals(df)
        # Eligible bars: [atr_period, n-2] inclusive  → n - 1 - atr_period
        assert len(sigs) == len(df) - 1 - params.atr_period

    def test_max_hold_bars_is_one_by_default(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=5, down_bars=5, n_cycles=20)
        sigs = OvernightDrift(OvernightDriftParams()).signals(df)
        for s in sigs:
            assert s.max_hold_bars == 1

    def test_does_not_emit_at_last_bar(self):
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=5, down_bars=5, n_cycles=20)
        sigs = OvernightDrift(OvernightDriftParams()).signals(df)
        last = len(df) - 1
        assert not any(s.bar_idx == last for s in sigs)


# ===========================================================================
# ORB strategy — specific tests
# ===========================================================================
class TestOrbStrategy:
    def test_no_signals_on_hourly_data(self):
        """If no bar lands at session_open time, ORB never fires."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=10, down_bars=10, n_cycles=5)
        sigs = Orb(OrbParams()).signals(df)
        assert sigs == []

    def test_long_breakout_fires_at_or_breakout_bar(self):
        df = _m15_session_candles(n_sessions=2, breakout_above=True)
        sigs = Orb(OrbParams()).signals(df)
        assert len(sigs) >= 1
        # First session: bars 0..25; breakout bar = bar 4 (14:30 UTC)
        first = sigs[0]
        assert first.bar_idx == 4
        assert first.direction == "LONG"
        # Stop = OR_low; entry > stop
        assert first.stop_price < first.entry_price < first.target_price

    def test_short_breakout_fires_when_close_below_or_low(self):
        df = _m15_session_candles(n_sessions=2, breakout_above=False)
        sigs = Orb(OrbParams()).signals(df)
        assert len(sigs) >= 1
        assert sigs[0].direction == "SHORT"
        assert sigs[0].target_price < sigs[0].entry_price < sigs[0].stop_price

    def test_long_only_skips_short_breakouts(self):
        df = _m15_session_candles(n_sessions=2, breakout_above=False)
        sigs = Orb(OrbParams(long_only=True)).signals(df)
        # No long breakouts in this fixture, so we expect zero signals
        assert all(s.direction == "LONG" for s in sigs)

    def test_at_most_one_signal_per_session(self):
        df = _m15_session_candles(n_sessions=3, breakout_above=True)
        sigs = Orb(OrbParams()).signals(df)
        # 3 sessions, ≤ 1 signal each
        assert len(sigs) <= 3

    def test_max_hold_bars_within_session(self):
        df = _m15_session_candles(n_sessions=2, breakout_above=True)
        sigs = Orb(OrbParams()).signals(df)
        bars_per_session = 26
        for s in sigs:
            session_start_bar = (s.bar_idx // bars_per_session) * bars_per_session
            last_session_bar = session_start_bar + bars_per_session - 1
            assert s.bar_idx + s.max_hold_bars <= last_session_bar

    def test_geometry_valid_on_real_breakouts(self):
        df = _m15_session_candles(n_sessions=4, breakout_above=True)
        for s in Orb(OrbParams()).signals(df):
            if s.direction == "LONG":
                assert s.stop_price < s.entry_price < s.target_price
            else:
                assert s.target_price < s.entry_price < s.stop_price


# ===========================================================================
# InsideBar strategy — specific tests
# ===========================================================================
class TestInsideBarStrategy:
    def _candles_with_inside_bar(self, breakout: str = "up") -> pd.DataFrame:
        """Build 6 bars: bar 0 normal, bar 1 mother, bar 2 inside, bar 3 break,
        bars 4-5 follow-through."""
        n = 6
        times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
        opens = np.array([100, 100, 100, 100, 100, 100], dtype=float)
        highs = np.array([100.5, 102.0, 101.5, 103.5, 104.0, 104.5], dtype=float)
        lows  = np.array([99.5,   98.0,  98.5,  99.0,  98.5,  98.0], dtype=float)
        closes = np.array([100.0, 100.0, 100.0,
                            103.0 if breakout == "up" else 97.0,
                            103.5 if breakout == "up" else 96.5,
                            104.0 if breakout == "up" else 96.0], dtype=float)
        if breakout == "down":
            # Make bar 3's close below bar 2's low (98.5) → SHORT trigger
            closes[3] = 97.5  # below 98.5 ✓
            lows[3] = 96.0
            highs[3] = 99.0
        # Verify bar 2 is inside bar 1: high[2]<=high[1] AND low[2]>=low[1]
        assert highs[2] <= highs[1] and lows[2] >= lows[1]
        return pd.DataFrame({
            "time": times, "open": opens,
            "high": highs, "low": lows, "close": closes,
            "volume": np.full(n, 1000.0),
        })

    def test_long_break_fires_on_close_above_inside_high(self):
        df = self._candles_with_inside_bar(breakout="up")
        sigs = InsideBar(InsideBarParams()).signals(df)
        assert any(s.bar_idx == 3 and s.direction == "LONG" for s in sigs)

    def test_short_break_fires_on_close_below_inside_low(self):
        df = self._candles_with_inside_bar(breakout="down")
        sigs = InsideBar(InsideBarParams()).signals(df)
        assert any(s.bar_idx == 3 and s.direction == "SHORT" for s in sigs)

    def test_long_only_skips_short_breaks(self):
        df = self._candles_with_inside_bar(breakout="down")
        sigs = InsideBar(InsideBarParams(long_only=True)).signals(df)
        assert all(s.direction == "LONG" for s in sigs)

    def test_stop_at_inside_low_for_long(self):
        df = self._candles_with_inside_bar(breakout="up")
        sigs = InsideBar(InsideBarParams()).signals(df)
        s = next(s for s in sigs if s.bar_idx == 3 and s.direction == "LONG")
        # Inside bar (bar 2) low = 98.5
        assert s.stop_price == 98.5

    def test_target_is_2R_default(self):
        df = self._candles_with_inside_bar(breakout="up")
        sigs = InsideBar(InsideBarParams()).signals(df)
        s = next(s for s in sigs if s.bar_idx == 3 and s.direction == "LONG")
        risk = s.entry_price - s.stop_price
        assert abs((s.target_price - s.entry_price) - 2 * risk) < 1e-9


# ===========================================================================
# Backtester max_hold_bars time-based exit — exact-PnL + reconciliation
# ===========================================================================
class TestMaxHoldExit:
    def test_time_close_at_correct_bar(self):
        """A signal at bar 5 with max_hold_bars=3 must close at bar 8's close."""
        from core.strategy import Signal
        n = 30
        times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
        closes = np.full(n, 100.0)
        # Make bar 8 have a close of 101 to verify exit price
        closes[8] = 101.0
        df = pd.DataFrame({
            "time": times, "open": closes,
            "high": closes + 0.5, "low": closes - 0.5,
            "close": closes, "volume": np.full(n, 1000.0),
        })
        sig = Signal(bar_idx=5, direction="LONG", entry_price=100.0,
                      stop_price=90.0, target_price=200.0, max_hold_bars=3)
        result = run_backtest(df, [sig], starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0)
        assert result.n_trades == 1
        t = result.trades[0]
        assert t.close_reason == "time"
        assert t.exit_bar_idx == 8
        # PnL = (101 - 100) × 0.1 × 1.0 = 0.10
        assert abs(t.realized_pnl - 0.10) < 1e-9
        assert result.reconciles

    def test_time_close_with_slippage_reconciles(self):
        """max_hold + slippage on exit must still reconcile."""
        from core.strategy import Signal
        n = 30
        times = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
        closes = np.full(n, 100.0)
        df = pd.DataFrame({
            "time": times, "open": closes,
            "high": closes + 0.5, "low": closes - 0.5,
            "close": closes, "volume": np.full(n, 1000.0),
        })
        sig = Signal(bar_idx=5, direction="LONG", entry_price=100.0,
                      stop_price=90.0, target_price=200.0, max_hold_bars=4)
        result = run_backtest(df, [sig], starting_balance=100_000,
                                lots=0.1, money_per_unit_price=1.0,
                                slippage_per_fill_atr_frac=0.2,
                                commission_per_trade=2.0)
        assert result.reconciles, (
            f"sum_pnl={result.sum_realized_pnl} eq_pnl={result.equity_curve_pnl}"
        )
        assert result.trades[0].close_reason == "time"
