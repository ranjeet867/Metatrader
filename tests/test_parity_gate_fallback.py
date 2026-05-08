"""Regression tests for the ParityGate DB-split + alias-resolver fix
(Bug A, shipped 2026-05-08).

Pre-fix the dashboards wrote parity_log rows to REPO/data/v2.db, but
LiveExecutor was instantiated with the per-account DB at
data/accounts/<login>/v2.db. Reader and writer were on different
files, so every live signal got `preflight_deny:parity_recent`.
Compounded by a strategy-name mismatch: parity rows used the base
strategy name (e.g. "rsi_meanrev") while the runner's deployment
strategy was a variant name ("rsi_30_70").

Post-fix the gate:
  - reads from primary DB AND from configured fallback DBs
  - resolves the strategy name through the registry to also try the
    base name (rsi_30_70 → rsi_meanrev)
  - mirrors writes to all configured DBs by default

Failure of any test below means we've regressed. Do not weaken these.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import storage
from core.parity_gate import ParityGate


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Tests ─────────────────────────────────────────────────────────


def test_recent_pass_in_primary_db_is_seen(tmp_path: Path):
    """Sanity: the simplest case still works."""
    primary = tmp_path / "primary.db"
    storage.init_schema(primary)
    gate = ParityGate(primary)
    gate.record_pass("rsi_meanrev", 0.0)
    assert gate.is_recent("rsi_meanrev", max_age_hours=24)


def test_pass_in_fallback_db_is_seen(tmp_path: Path):
    """Pre-fix: writer wrote to main DB, reader looked at account DB,
    saw nothing. Post-fix: gate falls back to main DB and finds it."""
    account_db = tmp_path / "account.db"
    main_db = tmp_path / "main.db"
    storage.init_schema(account_db)
    storage.init_schema(main_db)

    # Simulate the dashboard writing to the main DB only (no mirror,
    # to model the bug exactly as it existed pre-fix).
    main_gate = ParityGate(main_db)
    main_gate.record_pass("rsi_meanrev", 1e-12, mirror=False)
    # Account DB has zero rows — pre-fix this returned False.
    assert ParityGate(account_db).is_recent("rsi_meanrev") is False
    # With fallback configured, the runner's gate finds the row.
    runner_gate = ParityGate(account_db, fallback_db_paths=[main_db])
    assert runner_gate.is_recent("rsi_meanrev")


def test_strategy_alias_resolves_variant_to_base(tmp_path: Path):
    """The deployment strategy is "rsi_30_70" but parity rows were
    written under the base "rsi_meanrev". The resolver must bridge."""
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    gate = ParityGate(db)
    gate.record_pass("rsi_meanrev", 0.0)
    # Variant name should resolve to base via strategy_resolver.
    assert gate.is_recent("rsi_30_70", max_age_hours=24)


def test_alias_resolution_donchian_variants(tmp_path: Path):
    """donchian_20 / donchian_55 should resolve to donchian_breakout."""
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    gate = ParityGate(db)
    gate.record_pass("donchian_breakout", 0.0)
    assert gate.is_recent("donchian_20", max_age_hours=24)
    assert gate.is_recent("donchian_55", max_age_hours=24)


def test_alias_resolution_ema_cross_variants(tmp_path: Path):
    """ema_cross_9_20 / ema_cross_12_26 should resolve to ema_cross."""
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    gate = ParityGate(db)
    gate.record_pass("ema_cross", 0.0)
    assert gate.is_recent("ema_cross_9_20", max_age_hours=24)
    assert gate.is_recent("ema_cross_12_26", max_age_hours=24)


def test_max_age_filter_still_applies(tmp_path: Path):
    """A stale row in the fallback DB must NOT pass the freshness gate."""
    primary = tmp_path / "primary.db"
    fallback = tmp_path / "fallback.db"
    storage.init_schema(primary)
    storage.init_schema(fallback)
    # Write a 48h-old row directly to the fallback.
    stale_ts = (_now() - timedelta(hours=48)).isoformat()
    storage.record_parity_pass(fallback, "rsi_meanrev", stale_ts, 0.0)
    gate = ParityGate(primary, fallback_db_paths=[fallback])
    assert gate.is_recent("rsi_meanrev", max_age_hours=24) is False
    assert gate.is_recent("rsi_meanrev", max_age_hours=72)


def test_record_pass_mirrors_by_default(tmp_path: Path):
    """record_pass with mirror=True (default) writes to both DBs so the
    reader/writer split can heal as new passes accumulate."""
    primary = tmp_path / "primary.db"
    fallback = tmp_path / "fallback.db"
    storage.init_schema(primary)
    storage.init_schema(fallback)
    gate = ParityGate(primary, fallback_db_paths=[fallback])
    gate.record_pass("rsi_meanrev", 0.0)
    # Both DBs must now have a row.
    assert storage.last_parity_pass(primary, "rsi_meanrev") is not None
    assert storage.last_parity_pass(fallback, "rsi_meanrev") is not None


def test_record_pass_mirror_false_writes_primary_only(tmp_path: Path):
    primary = tmp_path / "primary.db"
    fallback = tmp_path / "fallback.db"
    storage.init_schema(primary)
    storage.init_schema(fallback)
    gate = ParityGate(primary, fallback_db_paths=[fallback])
    gate.record_pass("rsi_meanrev", 0.0, mirror=False)
    assert storage.last_parity_pass(primary, "rsi_meanrev") is not None
    assert storage.last_parity_pass(fallback, "rsi_meanrev") is None


def test_no_pass_anywhere_returns_false(tmp_path: Path):
    primary = tmp_path / "primary.db"
    fallback = tmp_path / "fallback.db"
    storage.init_schema(primary)
    storage.init_schema(fallback)
    gate = ParityGate(primary, fallback_db_paths=[fallback])
    assert gate.is_recent("rsi_meanrev") is False
    assert gate.last_pass_for("rsi_meanrev") is None


def test_picks_most_recent_across_dbs(tmp_path: Path):
    """If both DBs have a row, last_pass_for returns the newest."""
    primary = tmp_path / "primary.db"
    fallback = tmp_path / "fallback.db"
    storage.init_schema(primary)
    storage.init_schema(fallback)
    older = (_now() - timedelta(hours=10)).isoformat()
    newer = (_now() - timedelta(hours=2)).isoformat()
    storage.record_parity_pass(primary, "rsi_meanrev", older, 0.0)
    storage.record_parity_pass(fallback, "rsi_meanrev", newer, 0.0)
    gate = ParityGate(primary, fallback_db_paths=[fallback])
    last = gate.last_pass_for("rsi_meanrev")
    assert last is not None
    ts, _ = last
    # Compare to within 1 second
    expected = datetime.fromisoformat(newer)
    assert abs((ts - expected).total_seconds()) < 1


def test_fallback_paths_dedupe_against_primary(tmp_path: Path):
    """Same path passed as primary AND fallback should not be probed
    twice (no-op silent — but importantly, not crash)."""
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    gate = ParityGate(db, fallback_db_paths=[db])
    assert gate.fallback_db_paths == []


def test_missing_fallback_db_is_safe(tmp_path: Path):
    """A non-existent fallback path must not crash — init_schema
    creates it, and probing returns nothing."""
    primary = tmp_path / "primary.db"
    missing = tmp_path / "subdir" / "does_not_exist.db"
    storage.init_schema(primary)
    gate = ParityGate(primary, fallback_db_paths=[missing])
    gate.record_pass("rsi_meanrev", 0.0, mirror=False)
    assert gate.is_recent("rsi_meanrev")
