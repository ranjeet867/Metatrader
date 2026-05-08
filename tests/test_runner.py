"""
test_runner.py — atomic per-bar tick.

Verifies:
  - Signal at last-bar fires open
  - Signal NOT at last bar is ignored (live-mode discipline)
  - Idempotent: tick(view) twice with same view does NOT double-open
  - SL hit on the last bar's intrabar range closes via update_bar
  - In no_entry_window → signal scan is skipped (counted)
  - Forced-flat on a Friday close window via update_bar
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from core.paper_executor import PaperExecutor
from core.runner import make_idempotency_key, tick
from core.strategy import Signal
from core.time_guards import TimeGuardCfg


CFG = TimeGuardCfg(
    weekend_flat_all=True,
    daily_close_flat_classes=("stock", "index"),
    us_session_close_hhmm="20:00",
    flat_buffer_minutes=5,
    no_entry_minutes_before_close=30,
    asset_class_overrides={"index": ["US100.cash"], "fx": []},
)


def _two_day_h1(start="2026-05-04T00:00:00Z") -> pd.DataFrame:
    n = 48
    times = pd.date_range(start, periods=n, freq="h", tz="UTC")
    closes = 100.0 + np.arange(n, dtype=float) * 0.1
    return pd.DataFrame({
        "time": times,
        "open":  closes - 0.05,
        "high":  closes + 0.05,
        "low":   closes - 0.05,
        "close": closes,
        "volume": np.full(n, 1000.0),
    })


class _StubStrategy:
    """Returns a single LONG signal at a configured bar_idx, every call."""
    name = "stub"

    def __init__(self, sig_bar_idx: int, candles: pd.DataFrame):
        self._sig_bar = sig_bar_idx
        self._candles = candles

    def signals(self, view: pd.DataFrame):
        # Only fire if the configured bar_idx is within the view
        if self._sig_bar >= len(view):
            return []
        entry = float(view["close"].iloc[self._sig_bar])
        return [Signal(
            bar_idx=self._sig_bar, direction="LONG",
            entry_price=entry,
            stop_price=entry - 1.0,
            target_price=entry + 2.0,
            reason="stub_long",
        )]


def test_signal_at_last_bar_fires_open():
    df = _two_day_h1()
    view = df.iloc[: 12]      # 12 bars; last_idx = 11
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()

    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0)
    assert len(res.opens) == 1
    assert ex.has_position("US100.cash")


def test_circuit_breaker_blocks_open():
    """When block_opens_reason is set, the tick must NOT open a new
    position even if the signal fires. update_bar still runs, but the
    counter increments."""
    df = _two_day_h1()
    view = df.iloc[: 12]
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()
    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0,
               block_opens_reason="DAILY LOSS exceeded $4500")
    assert len(res.opens) == 0
    assert not ex.has_position("US100.cash")
    assert res.skipped_due_to_circuit_breaker == 1
    assert any("DAILY LOSS" in e for e in res.errors)


def test_position_guard_blocks_when_other_dep_holds_symbol():
    """When another deployment already has a position on the same
    symbol, the strict policy blocks the new open."""
    from core.position_guard import OpenPosition

    df = _two_day_h1()
    view = df.iloc[: 12]
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()
    snapshot = [OpenPosition(
        deployment_id="other_dep", symbol="US100.cash",
        side="LONG", lots=1.0, opened_at_utc="2026-05-03T12:00:00",
    )]
    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0,
               deployment_id="my_dep",
               open_positions_snapshot=snapshot,
               position_guard_policy="strict")
    assert len(res.opens) == 0
    assert res.skipped_due_to_position_guard == 1
    assert any("position_guard" in e for e in res.errors)


def test_position_guard_allows_when_no_collision():
    """No conflicting position → guard returns ALLOW → open fires."""
    from core.position_guard import OpenPosition

    df = _two_day_h1()
    view = df.iloc[: 12]
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()
    snapshot = [OpenPosition(
        deployment_id="other_dep", symbol="EURUSD",
        side="LONG", lots=1.0, opened_at_utc="2026-05-03T12:00:00",
    )]
    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0,
               deployment_id="my_dep",
               open_positions_snapshot=snapshot,
               position_guard_policy="strict")
    assert len(res.opens) == 1
    assert res.skipped_due_to_position_guard == 0


def test_signal_not_on_last_bar_ignored():
    """A signal whose bar_idx isn't the last bar of the view must NOT fire.
    This is the live-mode discipline."""
    df = _two_day_h1()
    view = df.iloc[: 12]      # last_idx = 11
    strat = _StubStrategy(sig_bar_idx=5, candles=df)   # signal at idx 5, not 11
    ex = PaperExecutor()

    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0)
    assert len(res.opens) == 0
    assert not ex.has_position("US100.cash")


def test_double_tick_does_not_double_open():
    """Calling tick twice on the SAME view must not result in two opens —
    the idempotency_key is identical, so the second call is a no-op."""
    df = _two_day_h1()
    view = df.iloc[: 12]
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()

    res1 = tick(view, ex, strat, symbol="US100.cash", tf="H1",
                money_per_unit_price=1.0, lots=1.0)
    res2 = tick(view, ex, strat, symbol="US100.cash", tf="H1",
                money_per_unit_price=1.0, lots=1.0)
    assert len(res1.opens) == 1
    assert len(res2.opens) == 0   # idempotency
    assert ex.n_open == 1


def test_sl_hit_on_last_bar_closes_position():
    """If a position is open and the last bar's low ≤ stop_price, update_bar
    closes it via the runner's first step."""
    df = _two_day_h1()
    ex = PaperExecutor()
    # Open a position manually at bar 5
    ex.open(symbol="US100.cash", direction="LONG",
            signal_entry_price=100.5, stop_price=100.0, target_price=102.0,
            lots=1.0, money_per_unit_price=1.0,
            idempotency_key="manual",
            opened_at_bar_idx=5, opened_at_utc="t",
            atr_at_signal_bar=0.0)
    # Tick on a view where the last bar's low pierces the stop
    view = df.iloc[: 8].copy()
    view.loc[view.index[-1], "low"] = 99.0       # below stop 100.0

    strat = _StubStrategy(sig_bar_idx=999, candles=df)   # no new signal
    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0)
    assert len(res.closes) == 1
    assert res.closes[0].close_reason == "stop"
    assert not ex.has_position("US100.cash")


def test_no_entry_window_skips_signal():
    """If the last bar's effective close falls in [19:30, 20:00] UTC, the
    signal scan is skipped and counted in the result."""
    df = _two_day_h1()
    # Bar at index 19 is Mon 19:00 → close at 20:00 (in no-entry window)
    view = df.iloc[: 20]
    strat = _StubStrategy(sig_bar_idx=19, candles=df)
    ex = PaperExecutor(time_guard_cfg=CFG)

    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0,
               time_guard_cfg=CFG)
    assert res.skipped_due_to_no_entry_window == 1
    assert len(res.opens) == 0


def test_no_entry_window_does_not_block_outside_window():
    df = _two_day_h1()
    # Bar at index 12 is Mon 12:00 → close at 13:00 (outside window)
    view = df.iloc[: 13]
    strat = _StubStrategy(sig_bar_idx=12, candles=df)
    ex = PaperExecutor(time_guard_cfg=CFG)

    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=1.0,
               time_guard_cfg=CFG)
    assert res.skipped_due_to_no_entry_window == 0
    assert len(res.opens) == 1


def test_forced_flat_on_friday_close_via_update_bar():
    """Position is open before Friday's close window; tick at the Fri 19:00
    bar should force-close via update_bar."""
    # Build Thu+Fri H1 candles (start Thu 2026-05-07 00:00)
    df = _two_day_h1(start="2026-05-07T00:00:00Z")
    ex = PaperExecutor(time_guard_cfg=CFG)
    # Open a position at Thursday 12:00 (bar idx 12)
    ex.open(symbol="EURUSD", direction="LONG",
            signal_entry_price=100.0, stop_price=99.0, target_price=110.0,
            lots=1.0, money_per_unit_price=1.0,
            idempotency_key="manual",
            opened_at_bar_idx=12, opened_at_utc="t")
    # Now tick at the Fri 19:00 bar (idx 24+19 = 43; bar.close 20:00)
    view = df.iloc[: 24 + 20]    # last_idx = 43
    assert view["time"].iloc[43].weekday() == 4  # Friday
    strat = _StubStrategy(sig_bar_idx=999, candles=df)
    res = tick(view, ex, strat, symbol="EURUSD", tf="H1",
               money_per_unit_price=1.0, lots=1.0,
               time_guard_cfg=CFG)
    assert len(res.closes) == 1
    assert res.closes[0].close_reason == "weekend_flat"


def test_idempotency_key_format():
    """Key must be deterministic + include strategy / symbol / tf / iso-time."""
    t = pd.Timestamp("2026-05-08T19:00:00Z")
    k = make_idempotency_key("vol_breakout", "US100.cash", "H1", t)
    assert k.startswith("vol_breakout:US100.cash:H1:")
    assert "2026-05-08" in k


# ---------------------------------------------------------------------------
# Phase 27: dynamic position sizing through the runner
# ---------------------------------------------------------------------------

def test_runner_uses_calc_lots_when_risk_pct_supplied():
    from core.position_sizer import SymbolInfo
    df = _two_day_h1()
    view = df.iloc[: 12]
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()
    us100 = SymbolInfo("US100.cash", tick_size=0.01, tick_value=0.01,
                        volume_step=0.1, volume_min=0.1, volume_max=100,
                        digits=2, contract_size=1.0)

    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0, lots=0.0,
               risk_pct=0.3, symbol_info=us100, account_balance=91_400)
    # The stub fires LONG with stop=close-1 → 1.0-pt stop → big lots
    assert len(res.opens) == 1
    assert res.opens[0].lots > 0


def test_runner_rejects_sizing_failure():
    """If calc_lots returns ok=False, the open is skipped + an error logged."""
    from core.position_sizer import SymbolInfo
    df = _two_day_h1()
    view = df.iloc[: 12]
    strat = _StubStrategy(sig_bar_idx=11, candles=df)
    ex = PaperExecutor()
    # tiny equity → can't even afford volume_min
    us100 = SymbolInfo("US100.cash", tick_size=0.01, tick_value=0.01,
                        volume_step=0.1, volume_min=0.1, volume_max=100,
                        digits=2, contract_size=1.0)
    res = tick(view, ex, strat, symbol="US100.cash", tf="H1",
               money_per_unit_price=1.0,
               risk_pct=0.01, symbol_info=us100,
               account_balance=10.0)   # $10 equity → can't size
    assert len(res.opens) == 0
    assert any("sizing rejected" in e for e in res.errors)
