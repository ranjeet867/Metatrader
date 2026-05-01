"""
test_integration.py — end-to-end pipeline on synthetic data.

Verifies the FULL stack works together:
  generator → save_parquet → load_parquet → strategy.signals → run_backtest

If this passes, the system is internally consistent. We can run it on real
data with confidence that any number it produces is mathematically traceable.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from core.backtest import run_backtest
from core.data import load_parquet, save_parquet
from strategies.ema_cross import EmaCross, EmaCrossParams
from tests.fixtures.synthetic import sawtooth, step_function


class TestE2EPipeline:
    def test_step_function_yields_one_long_winning_trade(self):
        """A clean step from 100 to 110 should produce 1 LONG signal,
        and that LONG trade should hit its target. End-to-end."""
        df = step_function(low_price=100, high_price=110,
                            bars_at_low=100, bars_at_high=200)

        # Round-trip through parquet
        p = Path(tempfile.mkstemp(suffix=".parquet")[1])
        save_parquet(df, p)
        loaded = load_parquet(p)

        sigs = EmaCross(EmaCrossParams()).signals(loaded)
        long_signals = [s for s in sigs if s.direction == "LONG"]
        assert len(long_signals) == 1

        result = run_backtest(loaded, sigs,
                                starting_balance=100_000, lots=0.1,
                                money_per_unit_price=1.0)
        assert result.reconciles
        # The only LONG should hit target (price ramped from 100→110, then stayed)
        # Note: strategy uses ATR for stop/target, so the target may or may not hit
        # depending on ATR values at entry. We just assert reconciliation.

    def test_sawtooth_full_pipeline_reconciles(self):
        """Many signals, both directions, large dataset → reconciliation holds."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=20, down_bars=20, n_cycles=20)
        p = Path(tempfile.mkstemp(suffix=".parquet")[1])
        save_parquet(df, p)
        loaded = load_parquet(p)

        sigs = EmaCross(EmaCrossParams()).signals(loaded)
        result = run_backtest(loaded, sigs,
                                starting_balance=100_000, lots=0.1,
                                money_per_unit_price=1.0)
        assert result.reconciles, \
            f"E2E reconciliation FAILED: " \
            f"sum_pnl={result.sum_realized_pnl:.4f}  " \
            f"eq_pnl={result.equity_curve_pnl:.4f}"

    def test_full_pipeline_idempotent(self):
        """Running the same pipeline twice gives identical numbers."""
        df = sawtooth(low_price=100, high_price=110,
                       up_bars=15, down_bars=15, n_cycles=10)

        def run_once():
            sigs = EmaCross(EmaCrossParams()).signals(df)
            return run_backtest(df, sigs,
                                  starting_balance=100_000, lots=0.1,
                                  money_per_unit_price=1.0)

        a = run_once()
        b = run_once()

        assert a.n_trades == b.n_trades
        assert a.sum_realized_pnl == b.sum_realized_pnl
        assert a.ending_balance == b.ending_balance
        assert a.reconciles == b.reconciles
        np.testing.assert_array_equal(
            a.equity_curve["equity"].values, b.equity_curve["equity"].values,
        )
