"""
test_parity_gate.py — replay-parity gate state.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.parity_gate import ParityGate


def _tmp_db() -> Path:
    return Path(tempfile.mkstemp(suffix=".db")[1])


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)


def test_no_pass_recorded_is_not_recent():
    g = ParityGate(_tmp_db())
    assert not g.is_recent("vol_breakout", now_utc=NOW)


def test_recent_pass_within_window():
    g = ParityGate(_tmp_db())
    g.record_pass("vol_breakout", divergence_dollars=0.001,
                   at_utc=NOW - timedelta(hours=2))
    assert g.is_recent("vol_breakout", max_age_hours=24, now_utc=NOW)


def test_old_pass_outside_window():
    g = ParityGate(_tmp_db())
    g.record_pass("vol_breakout", divergence_dollars=0.001,
                   at_utc=NOW - timedelta(hours=48))
    assert not g.is_recent("vol_breakout", max_age_hours=24, now_utc=NOW)


def test_only_latest_pass_used():
    g = ParityGate(_tmp_db())
    g.record_pass("ema_cross", 0.05,
                   at_utc=NOW - timedelta(hours=48))
    g.record_pass("ema_cross", 0.001,
                   at_utc=NOW - timedelta(hours=2))
    last = g.last_pass_for("ema_cross")
    assert last is not None
    assert last[1] == 0.001
