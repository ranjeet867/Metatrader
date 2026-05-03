"""
test_dashboard_smoke.py — verify the dashboard module imports cleanly and that
its pure helper functions return expected types/shapes.

We avoid actually starting a Streamlit runtime; we only import the module and
test the helpers that DO NOT require a Streamlit script context. This is enough
to catch import-time errors, broken type signatures, or accidental dependencies
on global Streamlit state in helper code.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pandas as pd
import pytest

import dashboards.control as ctrl
from core.backtest import run_backtest
from core.strategy import Signal


def test_module_imports_without_error():
    """If this test fails, the dashboard has a syntax / import problem."""
    assert ctrl.main is not None
    assert callable(ctrl.run_one)


def test_discover_strategies_returns_known_strategies():
    """All 11 strategies (5 base + 6 index-focused) must be discoverable."""
    strats = ctrl.discover_strategies()
    expected_min = {
        "ema_cross", "ema_pullback", "donchian_breakout", "rsi_meanrev",
        "bbands_meanrev",
        "ibs", "overnight_drift", "orb", "inside_bar", "vol_breakout",
        "first30_meanrev",
    }
    missing = expected_min - set(strats.keys())
    assert not missing, f"missing strategies: {missing}"
    # Each entry is (StrategyClass, ParamsClass-or-None)
    for name, (cls, pcls) in strats.items():
        assert hasattr(cls, "name") and cls.name == name
        assert hasattr(cls, "signals")
        if pcls is not None:
            assert dataclasses.is_dataclass(pcls)


def test_discover_data_returns_dict_of_dicts():
    """data/ should be discoverable as {ticker: {tf: path}}."""
    data_index = ctrl.discover_data()
    assert isinstance(data_index, dict)
    if data_index:
        ticker, tfs = next(iter(data_index.items()))
        assert isinstance(tfs, dict)
        for tf, path in tfs.items():
            assert isinstance(path, Path)
            assert path.exists()


def test_run_one_goes_through_run_backtest_and_reconciles():
    """Critical: run_one MUST produce the same result as calling run_backtest
    directly, AND must reconcile. This is the contract that lets the dashboard
    show backtest output without re-implementing PnL math."""
    data_index = ctrl.discover_data()
    if not data_index:
        pytest.skip("no data parquets in data/")
    # Pick the first available ticker/tf
    ticker = next(iter(data_index.keys()))
    tf = next(iter(data_index[ticker].keys()))
    df = pd.read_parquet(data_index[ticker][tf])
    if len(df) < 200:
        pytest.skip("too few bars to run a meaningful backtest")

    strats = ctrl.discover_strategies()
    StratCls, ParamsCls = strats["ema_cross"]
    strat = StratCls(ParamsCls()) if ParamsCls else StratCls()

    out = ctrl.run_one(
        df, strat,
        starting_balance=91_400, lots=0.1, money_per_unit=1.0,
        commission_per_trade=3.0, slippage_atr_frac=0.1,
        train_pct=0.6,
    )
    # Required keys for the dashboard
    for k in ("result", "signals", "train", "test", "split_idx", "candles"):
        assert k in out, f"run_one missing key: {k}"
    # Reconciliation invariant
    assert out["result"].reconciles
    # train + test should sum to total realized
    assert abs(
        (out["train"].sum_pnl + out["test"].sum_pnl)
        - out["result"].sum_realized_pnl
    ) < 1e-6


def test_trades_to_dataframe_returns_expected_columns():
    """trades_to_dataframe must produce columns the dashboard table expects."""
    n_bars = 30
    times = pd.date_range("2024-01-01", periods=n_bars, freq="h", tz="UTC")
    closes = [100.0] * n_bars
    df = pd.DataFrame({
        "time": times, "open": closes, "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes], "close": closes,
        "volume": [1000.0] * n_bars,
    })
    sig = Signal(bar_idx=5, direction="LONG", entry_price=100.0,
                  stop_price=99.0, target_price=200.0)
    r = run_backtest(df, [sig], starting_balance=10_000, lots=0.1,
                       money_per_unit_price=1.0)
    table = ctrl.trades_to_dataframe(r.trades, df)
    assert isinstance(table, pd.DataFrame)
    expected_cols = {"direction", "entry_time", "exit_time", "entry", "exit",
                      "pnl_$", "R", "reason", "bars_held"}
    assert expected_cols.issubset(set(table.columns)), \
        f"missing columns: {expected_cols - set(table.columns)}"
    assert len(table) == r.n_trades


def test_plotly_helpers_return_figures():
    """equity_figure, drawdown_figure, trade_reasons_figure must return go.Figure."""
    import plotly.graph_objects as go
    eq = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=10, freq="h", tz="UTC"),
        "equity": [100_000.0 + i for i in range(10)],
    })
    f1 = ctrl.equity_figure(eq, eq["time"].iloc[5], 100_000.0, "test")
    f2 = ctrl.drawdown_figure(eq)
    f3 = ctrl.trade_reasons_figure([])
    for f in (f1, f2, f3):
        assert isinstance(f, go.Figure)


def test_default_money_per_unit_covers_known_tickers():
    """Sanity: the lookup tables match the ones used by sweep_grid.py."""
    for t in ["US100.cash", "EU50.cash", "USDJPY", "EURUSD", "GBPJPY"]:
        assert t in ctrl.DEFAULT_MONEY_PER_UNIT
        assert t in ctrl.DEFAULT_LOTS


def test_freshness_summary_buckets_files_correctly(tmp_path):
    """freshness_summary should bucket files into <2d / 2-7d / >7d by mtime."""
    import os
    import time as time_mod
    # Build a tiny fake data_index pointing at three temp files with controlled mtimes
    fresh = tmp_path / "FRESH_D1.parquet"
    medium = tmp_path / "MEDIUM_D1.parquet"
    old = tmp_path / "OLD_D1.parquet"
    for f in (fresh, medium, old):
        f.write_bytes(b"x")
    now = time_mod.time()
    os.utime(fresh, (now, now - 3600))                  # 1h old → green
    os.utime(medium, (now, now - 86400 * 4))            # 4d old → yellow
    os.utime(old, (now, now - 86400 * 30))              # 30d old → red
    data_index = {
        "FRESH":  {"D1": fresh},
        "MEDIUM": {"D1": medium},
        "OLD":    {"D1": old},
    }
    rows, ng, ny, nr = ctrl.freshness_summary(data_index)
    assert ng == 1 and ny == 1 and nr == 1
    by_ticker = {r["ticker"]: r for r in rows}
    assert by_ticker["FRESH"]["bucket"] == "green"
    assert by_ticker["MEDIUM"]["bucket"] == "yellow"
    assert by_ticker["OLD"]["bucket"] == "red"
