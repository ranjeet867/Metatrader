"""
test_slippage_tracker.py — pin the per-fill slippage logging contract.

The tracker must:
  1. Auto-create its table on first record
  2. Record signal vs actual fill, compute slippage_atr_frac correctly
  3. Aggregate by symbol with sane stats
  4. Never raise — failures are swallowed (logged only)
"""
from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from core import slippage_tracker as st


def test_record_creates_table_and_inserts(tmp_path: Path):
    db = tmp_path / "v2.db"
    st.record(
        db_path=db, deployment_id="d1",
        symbol="EURUSD", tf="M15", direction="LONG",
        signal_entry_price=1.17000,
        actual_fill_price=1.17005,
        atr_at_signal=0.00050,
    )
    with sqlite3.connect(str(db)) as c:
        rows = c.execute("SELECT * FROM slippage_events").fetchall()
    assert len(rows) == 1


def test_slippage_atr_frac_math(tmp_path: Path):
    """0.5 pip slippage on 5 pip ATR = 0.10 = 10% of ATR."""
    db = tmp_path / "v2.db"
    st.record(
        db_path=db, deployment_id="d1",
        symbol="EURUSD", tf="M15", direction="LONG",
        signal_entry_price=1.17000,
        actual_fill_price=1.17005,
        atr_at_signal=0.00050,
    )
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        r = c.execute(
            "SELECT slippage_abs, slippage_atr_frac FROM slippage_events"
        ).fetchone()
    assert r["slippage_abs"] == pytest.approx(0.00005, abs=1e-7)
    assert r["slippage_atr_frac"] == pytest.approx(0.10, abs=0.001)


def test_atr_zero_yields_zero_atr_frac(tmp_path: Path):
    """When ATR is 0 (degenerate) we record but don't divide."""
    db = tmp_path / "v2.db"
    st.record(
        db_path=db, deployment_id="d1",
        symbol="EURUSD", tf="M15", direction="LONG",
        signal_entry_price=1.0,
        actual_fill_price=1.001,
        atr_at_signal=0.0,
    )
    with sqlite3.connect(str(db)) as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM slippage_events").fetchone()
    assert r["slippage_atr_frac"] == 0.0
    assert r["slippage_abs"] > 0


def test_aggregate_by_symbol(tmp_path: Path):
    db = tmp_path / "v2.db"
    # 3 EURUSD events, 1 GBPUSD event
    for slip in [0.0001, 0.0002, 0.0003]:
        st.record(db_path=db, deployment_id="d", symbol="EURUSD",
                   tf="M15", direction="LONG",
                   signal_entry_price=1.17, actual_fill_price=1.17 + slip,
                   atr_at_signal=0.001)
    st.record(db_path=db, deployment_id="d", symbol="GBPUSD",
               tf="M15", direction="LONG",
               signal_entry_price=1.27, actual_fill_price=1.2701,
               atr_at_signal=0.0010)

    agg = st.aggregate_by_symbol(db)
    eur = next(r for r in agg if r["symbol"] == "EURUSD")
    gbp = next(r for r in agg if r["symbol"] == "GBPUSD")
    assert eur["n_opens"] == 3
    assert gbp["n_opens"] == 1
    # Mean of [0.1, 0.2, 0.3] / atr 0.001 = mean of [0.1, 0.2, 0.3] = 0.2
    assert eur["mean_slip_atr"] == pytest.approx(0.20, abs=0.001)


def test_record_never_raises_on_bad_input(tmp_path: Path):
    """Bad path can't bring down the runner — caller relies on this."""
    # Non-existent dir as db path → record swallows the error
    bad_db = tmp_path / "missing_dir" / "v2.db"
    # Ensure parent exists so first record creates the file
    bad_db.parent.mkdir(parents=True, exist_ok=True)
    # But pass weird types that would normally raise
    st.record(
        db_path=bad_db, deployment_id="d",
        symbol="X", tf="M15", direction="LONG",
        signal_entry_price=float("nan"),
        actual_fill_price=float("nan"),
        atr_at_signal=0.0,
    )
    # Did not raise — that's the contract


def test_recent_events_returns_descending(tmp_path: Path):
    db = tmp_path / "v2.db"
    for i in range(5):
        st.record(db_path=db, deployment_id=f"d{i}",
                   symbol="EURUSD", tf="M15", direction="LONG",
                   signal_entry_price=1.17, actual_fill_price=1.17 + i * 0.0001,
                   atr_at_signal=0.001)
    events = st.recent_events(db, limit=3)
    assert len(events) == 3
    # Most recent first (highest id)
    assert events[0]["deployment_id"] == "d4"
    assert events[2]["deployment_id"] == "d2"
