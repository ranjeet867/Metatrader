"""
test_backup_all.py — pin the disaster-recovery backup behaviour.

The backup script writes a timestamped zip into ~/Documents (or a
configurable destination) so the user can sync it to Google Drive and
restore on a fresh laptop. Tests verify:

  • Required files (core/, strategies/, pyproject.toml, etc.) make it in
  • Excluded paths (__pycache__, .git, .pyc, .ex5) are NOT in the zip
  • A BACKUP_MANIFEST.txt with a UTC timestamp is embedded
  • Rotation deletes oldest backups when --keep is set
  • --skip-parquets shrinks the bundle (no .parquet files inside)
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "backup_all.py"


def _run_backup(dest: Path, *, extra: list[str] | None = None) -> Path:
    """Run scripts/backup_all.py with --dest pointing at tmp_path.
    Returns the path to the zip that was created."""
    cmd = [sys.executable, str(SCRIPT), "--dest", str(dest)]
    if extra:
        cmd.extend(extra)
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        f"backup script failed:\nstdout={result.stdout}\n"
        f"stderr={result.stderr}"
    )
    zips = list(dest.glob("mt5_v2_backup_*.zip"))
    assert zips, f"no zip produced in {dest}; stdout={result.stdout}"
    return zips[0]


def test_backup_creates_zip_in_destination(tmp_path):
    zip_path = _run_backup(tmp_path)
    assert zip_path.exists()
    assert zip_path.stat().st_size > 1024   # at least 1 KB


def test_backup_includes_required_files(tmp_path):
    zip_path = _run_backup(tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    # Code
    assert "core/backtest.py" in names
    assert "core/replay.py" in names
    assert "core/parity_check.py" in names
    assert "core/circuit_breaker.py" in names
    assert "core/position_guard.py" in names
    # Strategies
    assert any(n.startswith("strategies/") and n.endswith(".py")
                for n in names), "no strategy file in backup"
    # Configs
    assert "pyproject.toml" in names
    assert "Makefile" in names
    # Manifest
    assert "BACKUP_MANIFEST.txt" in names


def test_backup_excludes_caches_and_binaries(tmp_path):
    zip_path = _run_backup(tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = list(zf.namelist())
    bad = [
        n for n in names
        if "__pycache__/" in n
        or n.endswith(".pyc")
        or n.endswith(".ex5")
        or n.startswith(".git/")
        or "/.git/" in n
        or "egg-info/" in n
        or n.endswith(".DS_Store")
    ]
    assert not bad, f"backup contains forbidden files: {bad[:5]}"


def test_manifest_contains_timestamp_and_python_version(tmp_path):
    zip_path = _run_backup(tmp_path)
    with zipfile.ZipFile(zip_path) as zf:
        manifest = zf.read("BACKUP_MANIFEST.txt").decode()
    assert "created_utc=" in manifest
    assert "file_count=" in manifest
    assert "python=" in manifest
    assert "Restore steps" in manifest


def test_skip_parquets_excludes_parquet_files(tmp_path):
    zip_path = _run_backup(tmp_path, extra=["--skip-parquets"])
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    parquets = [n for n in names if n.endswith(".parquet")]
    assert not parquets, f"--skip-parquets still left {len(parquets)} parquets"


def test_keep_rotates_old_backups(tmp_path):
    """Run backup three times with --keep 2; only the last 2 should remain."""
    import time
    for _ in range(3):
        # Each run produces a new timestamped zip (sleep 1s for distinct UTC ts)
        time.sleep(1.1)
        _run_backup(tmp_path, extra=["--keep", "2"])
    backups = sorted(tmp_path.glob("mt5_v2_backup_*.zip"))
    assert len(backups) == 2, (
        f"expected 2 zips after rotation; got {len(backups)}"
    )


def test_keep_zero_does_not_rotate(tmp_path):
    """--keep 0 means rotation disabled."""
    import time
    for _ in range(2):
        time.sleep(1.1)
        _run_backup(tmp_path, extra=["--keep", "0"])
    backups = list(tmp_path.glob("mt5_v2_backup_*.zip"))
    assert len(backups) == 2
