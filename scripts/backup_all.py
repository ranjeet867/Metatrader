#!/usr/bin/env python3
"""
backup_all.py — full disaster-recovery backup of the trading platform.

Bundles into a single timestamped zip that you can sync to Google Drive:

  • All source code (core/, strategies/, dashboards/, scripts/, tests/, mql5/)
  • The SQLite trades journal (data/v2.db)
  • Every per-account config + deployments + circuit-breaker JSON
  • Every cached parquet (data/*.parquet)
  • Edge catalog markdowns + symbol_info JSONs
  • Top-level configs (pyproject.toml, Makefile, README, etc.)

What's excluded:
  • .git/ (pull from GitHub if you need history)
  • __pycache__, .pytest_cache, *.egg-info
  • Compiled MT5 binaries (.ex5) — recompile from .mq5 on the new machine
  • Anything in data/ matching .gitignore

Output: ~/Documents/mt5_v2_backup_<UTC-timestamp>.zip

After running, drag that file into your Google Drive folder. To restore
on a new laptop:
  unzip ~/Downloads/mt5_v2_backup_<ts>.zip -d ~/Documents/mt5_quant_trader_v2
  cd ~/Documents/mt5_quant_trader_v2
  pip install -e .
  make refresh-data    # re-fetches anything stale

Usage:
  python scripts/backup_all.py                      # default: ~/Documents
  python scripts/backup_all.py --dest /some/dir     # custom destination
  python scripts/backup_all.py --skip-parquets      # smaller, faster zip
  python scripts/backup_all.py --keep N             # rotate, keep last N
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# Patterns to skip when walking the repo
EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "pytest-cache-files-z4uwisc5",
    "mt5_quant_trader_v2.egg-info",
}
EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".log",
    ".tmp",
    ".swp",
    ".DS_Store",
    ".ex5",        # recompile from .mq5 on new machine
}


def _should_skip(path: Path) -> bool:
    """True if any part of the path is in EXCLUDE_DIRS or has an excluded suffix."""
    parts = set(path.parts)
    if parts & EXCLUDE_DIRS:
        return True
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    if path.name.startswith("."):
        # Hidden files (DS_Store etc.) but allow .gitignore, .env.example
        if path.name not in {".gitignore", ".env.example",
                                ".github", ".pre-commit-config.yaml"}:
            return True
    return False


def _walk_repo(repo: Path, *, skip_parquets: bool) -> list[Path]:
    """Yield every file we want in the backup, respecting exclusion rules."""
    files = []
    for root, dirnames, filenames in os.walk(repo):
        rel_root = Path(root).relative_to(repo)
        # In-place mutate dirnames so os.walk doesn't recurse into them
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for f in filenames:
            full = Path(root) / f
            rel = full.relative_to(repo)
            if _should_skip(rel):
                continue
            if skip_parquets and full.suffix == ".parquet":
                continue
            files.append(full)
    return files


def _print_size_summary(files: list[Path]) -> None:
    total = sum(f.stat().st_size for f in files if f.exists())
    by_ext: dict[str, tuple[int, int]] = {}
    for f in files:
        if not f.exists():
            continue
        ext = f.suffix or "(none)"
        cnt, sz = by_ext.get(ext, (0, 0))
        by_ext[ext] = (cnt + 1, sz + f.stat().st_size)
    print(f"\n  Total: {len(files)} files, {total / 1_048_576:.1f} MB")
    print(f"  Top extensions:")
    for ext, (cnt, sz) in sorted(by_ext.items(), key=lambda x: -x[1][1])[:8]:
        print(f"    {ext:<10} {cnt:>5} files   {sz / 1_048_576:>7.2f} MB")


def _rotate(dest_dir: Path, prefix: str, keep: int) -> int:
    """Delete older backups so only the newest `keep` files remain.
    Returns the number deleted."""
    if keep <= 0:
        return 0
    backups = sorted(
        dest_dir.glob(f"{prefix}*.zip"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = backups[keep:]
    for p in to_delete:
        try:
            p.unlink()
        except OSError as e:
            print(f"  ⚠ could not delete {p.name}: {e}")
    return len(to_delete)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Full backup zip of the mt5 trading platform.",
    )
    parser.add_argument(
        "--dest",
        default=str(Path.home() / "Documents"),
        help="Destination directory for the zip (default: ~/Documents).",
    )
    parser.add_argument(
        "--skip-parquets",
        action="store_true",
        help="Skip data/*.parquet (smaller zip; you can re-fetch via Data Manager).",
    )
    parser.add_argument(
        "--keep",
        type=int, default=10,
        help="After backup, keep this many newest zips and delete the rest. "
              "Default 10. Use 0 to never rotate.",
    )
    parser.add_argument(
        "--prefix", default="mt5_v2_backup_",
        help="Filename prefix for the zip (default: mt5_v2_backup_).",
    )
    args = parser.parse_args()

    dest_dir = Path(args.dest).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    zip_path = dest_dir / f"{args.prefix}{timestamp}.zip"

    print(f"📦  Backup of {REPO}")
    print(f"     → {zip_path}")
    if args.skip_parquets:
        print(f"     (parquets excluded)")

    files = _walk_repo(REPO, skip_parquets=args.skip_parquets)
    _print_size_summary(files)
    print()

    # Sanity check: do we have at least the core files?
    must_have = [
        REPO / "core" / "backtest.py",
        REPO / "strategies",
        REPO / "dashboards",
        REPO / "pyproject.toml",
    ]
    for p in must_have:
        if not p.exists():
            print(f"⛔ missing required path: {p.relative_to(REPO)}")
            return 2

    # Write the zip
    print("  Compressing...", end=" ", flush=True)
    with zipfile.ZipFile(zip_path, "w",
                          compression=zipfile.ZIP_DEFLATED,
                          compresslevel=6) as zf:
        for f in files:
            zf.write(f, arcname=f.relative_to(REPO).as_posix())
        # Embed a manifest for restore-time sanity check
        manifest_lines = [
            f"# mt5_v2_backup manifest",
            f"created_utc={timestamp}",
            f"source_repo={REPO}",
            f"file_count={len(files)}",
            f"skip_parquets={args.skip_parquets}",
            f"python={sys.version.split()[0]}",
            f"",
            f"# Restore steps:",
            f"#   1. unzip into ~/Documents/mt5_quant_trader_v2",
            f"#   2. cd into that dir",
            f"#   3. pip install -e .",
            f"#   4. (optional) make refresh-data",
            f"#   5. python scripts/sanity_check.py    # verifies imports",
        ]
        zf.writestr("BACKUP_MANIFEST.txt", "\n".join(manifest_lines) + "\n")
    print("done.")

    final_size = zip_path.stat().st_size / 1_048_576
    print(f"\n✅  Backup written: {zip_path.name}  ({final_size:.1f} MB)")
    print(f"     Sync this file to Google Drive / Dropbox / iCloud "
          f"to survive a laptop loss.")

    # Rotate
    if args.keep > 0:
        n_deleted = _rotate(dest_dir, args.prefix, args.keep)
        if n_deleted:
            print(f"     Rotated: kept {args.keep} newest backups, "
                  f"deleted {n_deleted} older.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
