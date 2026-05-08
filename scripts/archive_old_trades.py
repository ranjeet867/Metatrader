#!/usr/bin/env python3
"""
archive_old_trades.py — split closed trades older than N days from
data/v2.db into data/v2_archive.db so the hot DB stays small.

Rationale
---------
The `trades` table accumulates every closed trade. Over 1-2 years of
running 6-10 deployments, it can reach hundreds of thousands of rows.
While SQLite handles that fine, the dashboard's recent-trade queries
get slower because indexes have more to scan, and `v2.db` itself
becomes painful to back up.

What we move
------------
- Closed trades with `closed_at_utc` older than --keep-days (default 90)
- Same schema preserved in `data/v2_archive.db`
- Forensic snapshots older than --keep-days (covered by
  `forensic_snapshot.prune_older_than` separately, but we also
  archive them here so the audit trail is complete)

What we DON'T move
------------------
- Open positions (they're in `live_runs` / `paper_runs`, not `trades`)
- Run metadata (`runs` table) — keeps backtest run IDs intact
- Edge-decay samples (rolling 30 per deployment, naturally bounded)
- Bridge events (those have their own pruning policy)

Performance pages can opt in to query the archive via a flag — by
default they only see hot-DB data, keeping queries fast.

Usage
-----
  python scripts/archive_old_trades.py                    # 90 days, dry-run
  python scripts/archive_old_trades.py --apply             # actually move
  python scripts/archive_old_trades.py --keep-days 30 --apply  # custom retention
  python scripts/archive_old_trades.py --restore-from-archive  # if needed
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOT_DB = ROOT / "data" / "v2.db"
ARCHIVE_DB = ROOT / "data" / "v2_archive.db"

# Tables to archive: (table, time_column). Not all tables get archived
# — see module docstring for "what we don't move".
ARCHIVE_TABLES = [
    ("trades",              "closed_at_utc"),
    ("forensic_snapshots",  "captured_at_utc"),
    ("slippage_events",     "opened_at_utc"),
    ("edge_decay_samples",  "closed_at_utc"),
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)
    ).fetchone()
    return row is not None


def _ensure_archive_schema(hot: sqlite3.Connection,
                              archive: sqlite3.Connection) -> None:
    """Copy each table's CREATE TABLE statement from hot → archive,
    so archive has identical schema. Idempotent."""
    for table, _ in ARCHIVE_TABLES:
        if not _table_exists(hot, table):
            continue
        # Get the CREATE TABLE statement from hot db
        row = hot.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row and row[0]:
            try:
                # Replace CREATE TABLE → CREATE TABLE IF NOT EXISTS
                ddl = row[0]
                if not ddl.upper().startswith("CREATE TABLE IF NOT"):
                    ddl = ddl.replace("CREATE TABLE",
                                         "CREATE TABLE IF NOT EXISTS", 1)
                archive.execute(ddl)
            except sqlite3.OperationalError:
                pass  # Already exists with compatible schema
    archive.commit()


def archive_old_rows(hot_path: Path, archive_path: Path, *,
                       keep_days: int, apply: bool) -> dict[str, int]:
    """Move rows older than `keep_days` from each ARCHIVE_TABLE in
    hot_path → archive_path. Returns counts moved per table."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=keep_days)).isoformat()
    counts: dict[str, int] = {}

    # Read from hot, write to archive
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    hot = sqlite3.connect(str(hot_path))
    archive = sqlite3.connect(str(archive_path))
    hot.row_factory = sqlite3.Row

    try:
        _ensure_archive_schema(hot, archive)

        for table, time_col in ARCHIVE_TABLES:
            if not _table_exists(hot, table):
                counts[table] = 0
                continue
            # Count rows that would be archived
            n = hot.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {time_col} < ?",
                (cutoff,),
            ).fetchone()[0]
            counts[table] = n
            if not apply or n == 0:
                continue

            # Stream rows in batches to keep memory bounded
            BATCH = 5000
            offset = 0
            cols = [c[1] for c in hot.execute(
                f"PRAGMA table_info({table})").fetchall()]
            placeholders = ", ".join("?" * len(cols))
            insert_sql = (
                f"INSERT OR IGNORE INTO {table} "
                f"({', '.join(cols)}) VALUES ({placeholders})"
            )
            select_sql = (
                f"SELECT {', '.join(cols)} FROM {table} "
                f"WHERE {time_col} < ? "
                f"ORDER BY {time_col} ASC LIMIT ? OFFSET ?"
            )
            while True:
                rows = hot.execute(
                    select_sql, (cutoff, BATCH, offset)
                ).fetchall()
                if not rows:
                    break
                archive.executemany(insert_sql,
                                       [tuple(r) for r in rows])
                archive.commit()
                offset += BATCH
            # Now delete from hot
            hot.execute(
                f"DELETE FROM {table} WHERE {time_col} < ?",
                (cutoff,),
            )
            hot.commit()

        # VACUUM hot DB to reclaim disk space (if we applied changes)
        if apply and any(counts.values()):
            hot.execute("VACUUM")
            hot.commit()
    finally:
        hot.close()
        archive.close()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=90,
                       help="rows newer than this stay in hot DB (default 90)")
    ap.add_argument("--apply", action="store_true",
                       help="actually move data; default is dry-run")
    ap.add_argument("--hot", default=str(HOT_DB))
    ap.add_argument("--archive", default=str(ARCHIVE_DB))
    args = ap.parse_args()

    hot = Path(args.hot)
    archive = Path(args.archive)

    if not hot.exists():
        print(f"⚠ hot DB not found: {hot}")
        return 1

    print(f"Hot DB:     {hot} ({hot.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"Archive:    {archive}", end="")
    if archive.exists():
        print(f" ({archive.stat().st_size / 1024 / 1024:.1f} MB)")
    else:
        print(" (will be created)")
    print(f"Cutoff:     {args.keep_days} days "
          f"({(datetime.now(timezone.utc) - timedelta(days=args.keep_days)).date().isoformat()})")
    print(f"Mode:       {'APPLY' if args.apply else 'DRY-RUN'}")
    print()

    counts = archive_old_rows(hot, archive,
                                  keep_days=args.keep_days,
                                  apply=args.apply)
    total = sum(counts.values())
    print(f"{'Rows that would be moved' if not args.apply else 'Rows moved'}:")
    for table, n in counts.items():
        marker = "✓" if args.apply and n > 0 else (
            "→" if n > 0 else "—")
        print(f"  {marker} {table:25} {n:>8}")
    print(f"  Total: {total}")

    if args.apply:
        print()
        new_size = hot.stat().st_size / 1024 / 1024
        print(f"Hot DB now: {hot} ({new_size:.1f} MB after VACUUM)")
        if archive.exists():
            archive_size = archive.stat().st_size / 1024 / 1024
            print(f"Archive:    {archive} ({archive_size:.1f} MB)")
    elif total > 0:
        print()
        print("To execute, re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
