"""tests/test_embedded_rebaseline.py — scheduling logic for the
in-process rebaseline daemon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.embedded_rebaseline import (
    RebaselineConfig, _read_last_run, _write_last_run, is_due,
)


def _cfg(tmp_path: Path, days: int = 7) -> RebaselineConfig:
    return RebaselineConfig(
        repo_dir=tmp_path,
        interval_days=days,
        state_file=tmp_path / "state",
        safe_only=True,
    )


def test_is_due_when_state_file_missing(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # No prior run recorded → should fire immediately
    assert is_due(cfg) is True


def test_is_due_after_interval_elapsed(tmp_path: Path):
    cfg = _cfg(tmp_path, days=7)
    # Last run was 8 days ago → due
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    _write_last_run(cfg.resolved_state_file, eight_days_ago)
    assert is_due(cfg) is True


def test_not_due_when_recent(tmp_path: Path):
    cfg = _cfg(tmp_path, days=7)
    # Last run was 2 days ago → not due
    two_days_ago = datetime.now(timezone.utc) - timedelta(days=2)
    _write_last_run(cfg.resolved_state_file, two_days_ago)
    assert is_due(cfg) is False


def test_due_at_boundary(tmp_path: Path):
    cfg = _cfg(tmp_path, days=7)
    now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    _write_last_run(cfg.resolved_state_file, seven_days_ago)
    # Exactly 7 days = >= → due
    assert is_due(cfg, now=now) is True


def test_state_file_round_trips(tmp_path: Path):
    cfg = _cfg(tmp_path)
    when = datetime(2026, 5, 1, 9, 30, 0, tzinfo=timezone.utc)
    _write_last_run(cfg.resolved_state_file, when)
    got = _read_last_run(cfg.resolved_state_file)
    assert got is not None
    assert got == when


def test_state_file_corrupt_returns_none(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.resolved_state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.resolved_state_file.write_text("not-a-timestamp-blob")
    assert _read_last_run(cfg.resolved_state_file) is None
    # Corrupt = treat as never run = due
    assert is_due(cfg) is True


def test_disabled_thread_returns_none(tmp_path: Path):
    from core.embedded_rebaseline import start_rebaseline_thread
    t = start_rebaseline_thread(repo_dir=tmp_path, enabled=False)
    assert t is None


def test_resolved_state_file_default_path(tmp_path: Path):
    cfg = RebaselineConfig(repo_dir=tmp_path)
    assert cfg.resolved_state_file == tmp_path / "data" / ".last_rebaseline"
