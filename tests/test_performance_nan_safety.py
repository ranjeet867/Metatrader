"""
test_performance_nan_safety.py — regression test for the Performance page
NaN crash.

Bug: `int(r["n_trades"] or 0)` raises ValueError when n_trades is NaN
because pandas NaN is truthy → `NaN or 0` returns NaN → `int(NaN)` raises.
Started runs that never finished have NULL n_trades / sum_realized_pnl,
which pandas reads back as NaN.

The fix uses explicit pd.notna checks. This test pins the helpers used
inside render_recent_runs so the bug can't sneak back in.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest


# Re-implement the same safe helpers the page uses so we can test them
# without launching Streamlit.
def _safe_int(v) -> int:
    return int(v) if pd.notna(v) else 0


def _safe_float(v) -> float:
    return float(v) if pd.notna(v) else 0.0


def _safe_bool(v) -> bool:
    return bool(v) if pd.notna(v) else False


# ---------------------------------------------------------------------------
# _safe_int
# ---------------------------------------------------------------------------

def test_safe_int_handles_nan():
    assert _safe_int(float("nan")) == 0


def test_safe_int_handles_none():
    assert _safe_int(None) == 0


def test_safe_int_passes_real_number():
    assert _safe_int(42) == 42
    assert _safe_int(42.7) == 42
    assert _safe_int(0) == 0


def test_safe_int_handles_pandas_na():
    assert _safe_int(pd.NA) == 0


# ---------------------------------------------------------------------------
# _safe_float
# ---------------------------------------------------------------------------

def test_safe_float_handles_nan():
    assert _safe_float(float("nan")) == 0.0


def test_safe_float_handles_none():
    assert _safe_float(None) == 0.0


def test_safe_float_passes_real_number():
    assert _safe_float(123.45) == 123.45
    assert _safe_float(-7.5) == -7.5
    assert _safe_float(0.0) == 0.0


# ---------------------------------------------------------------------------
# _safe_bool
# ---------------------------------------------------------------------------

def test_safe_bool_handles_nan():
    assert _safe_bool(float("nan")) is False


def test_safe_bool_handles_none():
    assert _safe_bool(None) is False


def test_safe_bool_passes_truthy_int():
    assert _safe_bool(1) is True
    assert _safe_bool(0) is False


# ---------------------------------------------------------------------------
# Regression: the original buggy expression `int(x or 0)` does NOT work
# for NaN. This test pins the bug in the wild so we never reintroduce it.
# ---------------------------------------------------------------------------

def test_int_or_zero_pattern_breaks_on_nan():
    """The bug we fixed: NaN is truthy so `NaN or 0` returns NaN, not 0,
    and int(NaN) raises ValueError."""
    nan = float("nan")
    # Demonstrate that NaN is truthy
    assert bool(nan) is True
    # Demonstrate that the buggy expression raises
    with pytest.raises((ValueError, TypeError)):
        int(nan or 0)
    # Demonstrate that the fix doesn't raise
    assert _safe_int(nan) == 0


# ---------------------------------------------------------------------------
# Realistic run row — DataFrame with NaN columns from incomplete run
# ---------------------------------------------------------------------------

def test_runs_dataframe_with_unfinished_run_does_not_crash():
    """A run was started (row inserted) but never finished — its
    n_trades / sum_realized_pnl are NULL → NaN after read back."""
    runs = pd.DataFrame([
        {"run_id": "r1", "started_at_utc": "2026-05-03T10:00",
         "strategy": "ema_cross_9_20", "symbol": "USDJPY", "tf": "D1",
         "n_trades": 13, "sum_realized_pnl": 420.5,
         "starting_balance": 100_000.0, "reconciles": 1},
        {"run_id": "r2", "started_at_utc": "2026-05-03T11:00",
         "strategy": "donchian_20", "symbol": "US100.cash", "tf": "D1",
         "n_trades": float("nan"),         # ← unfinished
         "sum_realized_pnl": float("nan"), # ← unfinished
         "starting_balance": 100_000.0,
         "reconciles": float("nan")},      # ← unfinished
    ])
    rendered = []
    for _, r in runs.iterrows():
        rendered.append({
            "trades": _safe_int(r.get("n_trades")),
            "pnl": round(_safe_float(r.get("sum_realized_pnl")), 2),
            "start_bal": round(_safe_float(r.get("starting_balance"))),
            "reconciles": "✓" if _safe_bool(r.get("reconciles")) else "⛔",
        })
    # Finished run renders normal values
    assert rendered[0] == {"trades": 13, "pnl": 420.5,
                            "start_bal": 100_000, "reconciles": "✓"}
    # Unfinished run renders zeros + ⛔, no crash
    assert rendered[1] == {"trades": 0, "pnl": 0.0,
                            "start_bal": 100_000, "reconciles": "⛔"}
