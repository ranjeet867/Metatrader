"""
test_idempotency_real_data.py — INVARIANT-4: same input + same code →
byte-identical output.

Proves it on real cached data (US100.cash D1) by md5-hashing the trade list
twice and asserting the digests match.

Also runs the same check on the replay path so we know paper/live will be
bit-exact too.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.backtest import run_backtest
from core.data import load_parquet
from core.replay import replay_run
from strategies.vol_breakout import VolBreakout, VolBreakoutParams


REPO = Path(__file__).resolve().parents[1]

# These tests need the cached broker parquet to run. CI doesn't have it
# (.gitignored), so skip cleanly with a clear reason. Locally the files
# are present and tests run as normal.
_REQUIRED = REPO / "data" / "US100.cash_D1.parquet"
pytestmark = pytest.mark.skipif(
    not _REQUIRED.exists(),
    reason=f"requires {_REQUIRED.name} — run `make refresh-data` to populate",
)


def _hash_trades(trades) -> str:
    return hashlib.md5(repr(trades).encode("utf-8")).hexdigest()


def test_run_backtest_idempotent_real_data():
    candles = load_parquet(REPO / "data" / "US100.cash_D1.parquet")
    strat = VolBreakout(VolBreakoutParams(long_only=True))
    sigs = strat.signals(candles)

    args = dict(starting_balance=100_000, lots=1.0,
                money_per_unit_price=1.0,
                commission_per_trade=3.0,
                slippage_per_fill_atr_frac=0.1)
    a = run_backtest(candles, sigs, **args)
    b = run_backtest(candles, sigs, **args)

    assert _hash_trades(a.trades) == _hash_trades(b.trades), (
        "INVARIANT-4 violated: re-running run_backtest produced different "
        "trades on the same input"
    )
    # equity curves must match too
    assert a.equity_curve.equals(b.equity_curve)
    # And both reconcile
    assert a.reconciles and b.reconciles


def test_replay_run_idempotent_real_data():
    candles = load_parquet(REPO / "data" / "US100.cash_D1.parquet")
    strat_a = VolBreakout(VolBreakoutParams(long_only=True))
    strat_b = VolBreakout(VolBreakoutParams(long_only=True))

    args = dict(symbol="US100.cash", tf="D1",
                starting_balance=100_000, lots=1.0,
                money_per_unit_price=1.0,
                commission_per_trade=3.0,
                slippage_per_fill_atr_frac=0.1)
    a = replay_run(candles, strat_a, **args)
    b = replay_run(candles, strat_b, **args)

    assert _hash_trades(a.trades) == _hash_trades(b.trades)
    assert a.reconciles and b.reconciles
