"""
data_sync.py — keep `data/<TICKER>_<TF>.parquet` in sync with live MT5.

Why this module exists
----------------------
Pre-fix the system had TWO views of "what bars exist":
  - LIVE RUNNER: pulls 500 bars from MT5 via `copy_rates` each tick.
    Always up-to-date. Used for signal detection.
  - DASHBOARD / BACKTEST / REPLAY: reads `data/EURUSD_M15.parquet`
    on disk. Stale until user runs `make refresh-data` manually.

Symptom: dashboard says "last bar 2026-05-05 01:00 UTC", runner sees
"2026-05-05 07:00 UTC". User reasonably concludes the runner is
broken when in reality the runner is fine and the dashboard is
showing him 6h-stale data.

Fix
---
Single source of truth: the parquet IS the runner's data. After each
copy_rates fetch, any bars newer than the parquet's last_bar are
appended atomically (tmp-file + rename so concurrent dashboard reads
never see partial state). The dashboard then reads the same bars the
runner saw, milliseconds later.

Public API
----------
  sync_to_now(ticker, tf, fetcher, *, parquet_path=None, count=500)
      → pd.DataFrame

  fetcher: a callable (ticker, tf, n_bars) -> DataFrame — the same
  candle-fetcher the runner already has. We don't import the bridge
  client directly so this module stays decoupled and testable.

  count: how many bars to ask the bridge for. Default 500 covers most
  strategies (RSI-14, ATR-14, EMA-200). On startup catch-up the runner
  may pass a larger count to fill a longer gap.

Atomicity
---------
Write to `<path>.tmp` then `os.replace(tmp, path)` — POSIX rename is
atomic. A reader either sees the old file or the new one, never half.

What this module does NOT do
----------------------------
  - It does NOT bypass the bridge. The fetcher arg is the bridge call.
  - It does NOT lock or coordinate concurrent writers — single-runner
    architecture means at most one process is writing per (ticker, tf).
  - It does NOT validate the catalog — that's the dashboard's job.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import pandas as pd

from core.data import load_parquet, save_parquet

log = logging.getLogger(__name__)

# Default repo-root data dir. Tests pass an explicit path.
_DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# Required columns for a healthy candle DataFrame
_REQUIRED_COLS = ("time", "open", "high", "low", "close", "volume")


def parquet_path_for(ticker: str, tf: str,
                      *, data_dir: Path | None = None) -> Path:
    """Return `data_dir/<ticker>_<tf>.parquet`. Same convention used
    everywhere in the repo (data_manager, sweep, rebaseline, etc.)."""
    if data_dir is None:
        data_dir = _DEFAULT_DATA_DIR
    return Path(data_dir) / f"{ticker}_{tf}.parquet"


def _coerce_candles(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a fresh-from-bridge DataFrame: UTC timestamps, sorted,
    deduped on `time`, only the canonical columns. Returns a copy.

    Raises ValueError if required columns are missing — callers should
    catch this and skip the sync rather than corrupting the parquet.
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=list(_REQUIRED_COLS))
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    out = df[list(_REQUIRED_COLS)].copy()
    if not pd.api.types.is_datetime64_any_dtype(out["time"]):
        out["time"] = pd.to_datetime(out["time"], utc=True, errors="coerce")
    if out["time"].dt.tz is None:
        out["time"] = out["time"].dt.tz_localize("UTC")
    else:
        out["time"] = out["time"].dt.tz_convert("UTC")
    out = out.dropna(subset=["time"])
    out = out.drop_duplicates(subset=["time"], keep="last")
    out = out.sort_values("time").reset_index(drop=True)
    return out


def _atomic_save(df: pd.DataFrame, path: Path) -> None:
    """Write to <path>.tmp then rename to <path>. Concurrent readers
    see either the old file or the new one, never a half-written one.

    `save_parquet` writes to `path` directly so we use a temp path
    next to the destination (same filesystem → rename is atomic).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    save_parquet(df, tmp)
    os.replace(tmp, path)   # POSIX-atomic rename


def sync_to_now(
    ticker: str,
    tf: str,
    fetcher: Callable[[str, str, int], pd.DataFrame],
    *,
    parquet_path: Path | None = None,
    count: int = 500,
    data_dir: Path | None = None,
) -> pd.DataFrame:
    """Fetch live candles via `fetcher`, merge into the on-disk parquet,
    and return the merged DataFrame ready for signal computation.

    Behaviour:
      - If parquet does not exist: write the fetched df as the new
        parquet (initial fetch).
      - If parquet exists and fetcher returns 0 new bars: NO write.
        Returns the existing parquet contents unchanged.
      - If fetcher returns N new bars: append them, dedupe on `time`,
        atomic write, return merged.
      - If fetcher fails or returns malformed data: fall back to the
        existing parquet (don't corrupt good data with bad fetch).

    `parquet_path` overrides the default repo-root path (used by
    tests). `data_dir` is shorthand to pick a different root but keep
    the standard `<ticker>_<tf>.parquet` filename.
    """
    if parquet_path is None:
        parquet_path = parquet_path_for(ticker, tf, data_dir=data_dir)
    parquet_path = Path(parquet_path)

    # 1. Existing on-disk view (may be empty / missing)
    existing: pd.DataFrame | None = None
    if parquet_path.exists():
        try:
            existing = load_parquet(parquet_path)
        except Exception as e:
            log.warning("data_sync: parquet %s unreadable (%s) — "
                        "treating as empty",
                        parquet_path.name, e)
            existing = None

    # 2. Live fetch
    try:
        fresh = fetcher(ticker, tf, int(count))
    except Exception as e:
        log.warning("data_sync: fetcher(%s, %s, %d) raised: %s — "
                    "returning existing parquet",
                    ticker, tf, count, e)
        return existing if existing is not None else pd.DataFrame(
            columns=list(_REQUIRED_COLS)
        )

    try:
        fresh = _coerce_candles(fresh)
    except ValueError as e:
        log.warning("data_sync: bridge returned malformed candles "
                    "(%s) — returning existing parquet", e)
        return existing if existing is not None else pd.DataFrame(
            columns=list(_REQUIRED_COLS)
        )

    if fresh.empty:
        # Bridge returned nothing — return whatever was on disk
        return existing if existing is not None else fresh

    # 3. Merge
    if existing is None or existing.empty:
        merged = fresh
        new_bars = len(merged)
    else:
        existing_last = existing["time"].iloc[-1]
        new_only = fresh[fresh["time"] > existing_last]
        if new_only.empty:
            # No new bars — runner caught up between ticks. Return
            # existing without touching disk (cheap path, common).
            return existing
        merged = pd.concat([existing, new_only], ignore_index=True)
        new_bars = len(new_only)

    # 4. Atomic write
    try:
        _atomic_save(merged, parquet_path)
        log.info("data_sync: %s_%s.parquet +%d bar(s), now %d total",
                 ticker, tf, new_bars, len(merged))
    except Exception as e:
        log.exception("data_sync: failed to write %s: %s",
                      parquet_path, e)
        # Even if write failed, return the merged in-memory df so
        # the runner's signal computation isn't blocked.

    return merged
