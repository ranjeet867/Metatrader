"""
test_equity_tracker.py — pin the regime-classification math.

The recovery_factor multiplier protects against ramping risk back up
during oscillating recoveries — if the math here breaks, the runner
will over-size during fragile recoveries and bleed.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from core import equity_tracker as et


# ─── Recording + loading samples ──────────────────────────────────────
def test_record_and_load(tmp_path: Path):
    db = tmp_path / "v2.db"
    for v in [100_000, 100_500, 101_000, 99_500]:
        et.record_sample(db, equity=float(v))
    samples = et.load_recent_samples(db, limit=10)
    assert samples == [100_000.0, 100_500.0, 101_000.0, 99_500.0]


def test_max_samples_trim(tmp_path: Path):
    """Older samples beyond _MAX_SAMPLES get trimmed automatically."""
    db = tmp_path / "v2.db"
    # Patch the constant for this test (force small trim for speed)
    original = et._MAX_SAMPLES
    et._MAX_SAMPLES = 50
    try:
        for i in range(120):
            et.record_sample(db, equity=100_000.0 + i)
        samples = et.load_recent_samples(db, limit=200)
        # Should be capped at 50
        assert len(samples) == 50
        # And contain the most-recent values
        assert samples[-1] == 100_000.0 + 119
    finally:
        et._MAX_SAMPLES = original


# ─── NORMAL regime ────────────────────────────────────────────────────
def test_classify_normal_steady_growth():
    """Steadily rising equity above baseline → normal (not stable_peak
    because we haven't been above baseline long enough)."""
    samples = [100_000 + i for i in range(50)]   # 100k → 100049
    r = et.classify_regime(samples, baseline_equity=100_000)
    assert r.regime == et.EquityRegime.NORMAL
    assert r.recovery_factor == 1.0


def test_classify_no_samples():
    r = et.classify_regime([], baseline_equity=100_000)
    assert r.regime == et.EquityRegime.NORMAL
    assert r.recovery_factor == 1.0


# ─── FRAGILE_RECOVERY regime ──────────────────────────────────────────
def test_classify_fragile_recovery():
    """User's example: 91k → 93k → 91k → 93k → 91k. Multiple
    down-crossings of baseline → fragile."""
    # Build samples that oscillate around 100k baseline
    # Pattern: above baseline, down-cross, above, down-cross, ending below
    samples = (
        [100_500] * 20    # above baseline
        + [99_500] * 20   # down-cross #1
        + [100_500] * 20  # above
        + [99_500] * 20   # down-cross #2
        + [99_000] * 10   # below at end
    )
    r = et.classify_regime(samples, baseline_equity=100_000,
                            fragile_lookback=200)
    assert r.regime == et.EquityRegime.FRAGILE_RECOVERY
    assert r.recovery_factor == 0.5
    assert "down-crossings" in r.reason


def test_classify_fragile_only_when_currently_below():
    """If oscillations happened but current is above baseline, NOT fragile."""
    samples = (
        [100_500] * 20
        + [99_500] * 20    # down-cross #1
        + [100_500] * 20
        + [99_500] * 20    # down-cross #2
        + [101_000] * 10   # above at end → not fragile right now
    )
    r = et.classify_regime(samples, baseline_equity=100_000)
    assert r.regime == et.EquityRegime.NORMAL


# ─── STABLE_PEAK regime ───────────────────────────────────────────────
def test_classify_stable_peak():
    """Long sustained run above baseline+1% → peak factor."""
    # 720 samples ≥ 101k (1% above 100k baseline)
    samples = [101_500.0] * 800
    r = et.classify_regime(
        samples, baseline_equity=100_000,
        stable_peak_above_baseline_pct=1.0,
        stable_peak_min_samples_above=720,
    )
    assert r.regime == et.EquityRegime.STABLE_PEAK
    assert r.recovery_factor == 1.5


def test_classify_not_stable_peak_if_one_dip():
    """Even one dip below the threshold disqualifies stable_peak."""
    samples = [101_500.0] * 700 + [100_900.0] + [101_500.0] * 50
    r = et.classify_regime(
        samples, baseline_equity=100_000,
        stable_peak_above_baseline_pct=1.0,
        stable_peak_min_samples_above=720,
    )
    assert r.regime != et.EquityRegime.STABLE_PEAK


# ─── End-to-end via assess_now ────────────────────────────────────────
def test_assess_now_e2e(tmp_path: Path):
    """Integration: write samples, classify via the convenience helper."""
    db = tmp_path / "v2.db"
    # Build a fragile-recovery sample
    pattern = (
        [100_500] * 20
        + [99_500] * 20
        + [100_500] * 20
        + [99_500] * 20
        + [99_000] * 10
    )
    for v in pattern:
        et.record_sample(db, equity=float(v))
    r = et.assess_now(db, baseline_equity=100_000)
    assert r.regime == et.EquityRegime.FRAGILE_RECOVERY
    assert r.recovery_factor == 0.5
    assert r.n_samples == len(pattern)
