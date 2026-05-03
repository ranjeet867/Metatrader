"""
test_backtest_with_risk_pct.py — dynamic per-trade lots in run_backtest.

When risk_pct + symbol_info are passed, every trade's lots are computed
from calc_lots(running_balance, risk_pct, entry, stop, sym). Tests:
  - dynamic lots vary with running balance
  - reconciliation invariant still holds
  - sized backtest's PnL differs from a fixed-lots run (sanity)
  - skip-for-sizing case (tiny stop → too few lots → reject) is counted
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.backtest import run_backtest
from core.position_sizer import SymbolInfo
from core.strategy import Signal


US100 = SymbolInfo(
    name="US100.cash", tick_size=0.01, tick_value=0.01,
    volume_step=0.1, volume_min=0.1, volume_max=100.0,
    digits=2, contract_size=1.0,
)


def _h1_long_winners(n: int = 10) -> tuple[pd.DataFrame, list[Signal]]:
    """Synthetic candles where bar 1 fires LONG that hits target on bar 2."""
    times = pd.date_range("2026-05-04T00:00:00Z", periods=n, freq="h", tz="UTC")
    closes = np.arange(n, dtype=float) * 100 + 18_000   # 18000, 18100, ...
    df = pd.DataFrame({
        "time": times,
        "open":  closes - 5,
        "high":  closes + 50,
        "low":   closes - 5,
        "close": closes,
        "volume": np.full(n, 1000.0),
    })
    sigs = [
        Signal(bar_idx=1, direction="LONG",
                entry_price=18_100, stop_price=18_030,    # 70-pt stop
                target_price=18_240),                       # 140-pt target
    ]
    return df, sigs


def test_risk_pct_path_uses_dynamic_lots():
    df, sigs = _h1_long_winners()
    r = run_backtest(
        df, sigs,
        starting_balance=91_400, money_per_unit_price=1.0,
        symbol="US100.cash",
        risk_pct=0.3, symbol_info=US100,
    )
    assert r.n_trades == 1
    # 70-pt stop on US100 at 0.3% risk on $91,400 → 3.9 lots
    assert r.trades[0].lots == pytest.approx(3.9)
    # Reconciliation must hold
    assert r.reconciles, f"div={r.reconcile_divergence}"


def test_risk_pct_reconciles_on_loser_too():
    """Make the trade a loser (target unreachable, SL hit) — same sizing
    logic, must still reconcile."""
    df, sigs = _h1_long_winners()
    # Override stop so it gets hit early
    sigs = [Signal(bar_idx=1, direction="LONG",
                     entry_price=18_100, stop_price=18_080,    # 20-pt stop
                     target_price=99_999)]                       # unreachable
    df.loc[2, "low"] = 18_050   # bar 2 pierces stop
    r = run_backtest(
        df, sigs,
        starting_balance=91_400, money_per_unit_price=1.0,
        symbol="US100.cash",
        risk_pct=0.3, symbol_info=US100,
    )
    assert r.n_trades == 1
    assert r.trades[0].close_reason == "stop"
    assert r.reconciles


def test_lots_vary_with_running_balance():
    """Across multiple trades, lots change as the balance compounds.
    With sizing_uses_running_balance=True (default), a winning streak
    grows balance → lots increase."""
    n = 12
    times = pd.date_range("2026-05-04T00:00:00Z", periods=n, freq="h", tz="UTC")
    closes = np.arange(n, dtype=float) * 100 + 18_000
    df = pd.DataFrame({
        "time": times,
        "open":  closes - 5, "high":  closes + 100,
        "low":   closes - 5, "close": closes,
        "volume": np.full(n, 1000.0),
    })
    # Two LONG winners: bars 1 and 5 (both hit a 100-pt target on the next bar)
    sigs = [
        Signal(bar_idx=1, direction="LONG",
                entry_price=18_100, stop_price=18_030,
                target_price=18_180),
        Signal(bar_idx=5, direction="LONG",
                entry_price=18_500, stop_price=18_430,
                target_price=18_580),
    ]
    r = run_backtest(
        df, sigs,
        starting_balance=91_400, money_per_unit_price=1.0,
        symbol="US100.cash",
        risk_pct=0.3, symbol_info=US100,
        sizing_uses_running_balance=True,
    )
    assert r.n_trades == 2
    # Second trade's lots should be >= first because balance grew (or equal
    # if the rounding step kept it the same).
    lots_t1 = r.trades[0].lots
    lots_t2 = r.trades[1].lots
    assert lots_t2 >= lots_t1, (
        f"expected lots_t2 ({lots_t2}) >= lots_t1 ({lots_t1}) "
        "after a winning trade compounded balance"
    )
    assert r.reconciles


def test_below_min_lot_skips_signal():
    """Tiny equity / huge stop → calc_lots rejects → trade is skipped."""
    df, _ = _h1_long_winners()
    # Stop SO wide that 1% risk on $100 wouldn't even buy 0.1 lots.
    sigs = [Signal(bar_idx=1, direction="LONG",
                     entry_price=18_100, stop_price=10_000,  # 8100-pt stop!
                     target_price=18_500)]
    r = run_backtest(
        df, sigs,
        starting_balance=100, money_per_unit_price=1.0,
        symbol="US100.cash",
        risk_pct=0.1, symbol_info=US100,
    )
    assert r.n_trades == 0
    # Reconciliation trivially holds (no trades)
    assert r.reconciles


def test_fixed_lots_path_unchanged_when_risk_pct_none():
    """Backwards compat — passing only `lots` produces the same result as
    before Phase 27."""
    df, sigs = _h1_long_winners()
    r = run_backtest(
        df, sigs,
        starting_balance=91_400, lots=2.0, money_per_unit_price=1.0,
    )
    assert r.n_trades == 1
    assert r.trades[0].lots == pytest.approx(2.0)
    assert r.reconciles


def test_sized_path_pnl_differs_from_fixed():
    """A risk_pct=0.3% sized run on $91,400 produces ~3.9 lots; a
    fixed lots=1.0 run produces 1.0 lots. The PnL of the same trade must
    therefore differ by a factor of ~3.9."""
    df, sigs = _h1_long_winners()
    fixed = run_backtest(df, sigs, starting_balance=91_400, lots=1.0,
                          money_per_unit_price=1.0)
    sized = run_backtest(df, sigs, starting_balance=91_400,
                          money_per_unit_price=1.0,
                          symbol="US100.cash",
                          risk_pct=0.3, symbol_info=US100)
    # 3.9 lots / 1.0 lot ≈ 3.9× the dollar PnL
    ratio = sized.trades[0].realized_pnl / fixed.trades[0].realized_pnl
    assert 3.5 < ratio < 4.3, f"unexpected lots ratio: {ratio}"
