"""
equity_tracker.py — track rolling equity, detect recovery patterns,
emit a `recovery_factor` for adaptive_risk.

The problem
-----------
User described the pattern: account goes 91k → 93k → 91k. Static
risk sizing keeps offering full risk on each "recovery", but the
recovery isn't durable — every bounce is followed by another fall.
A senior trader would shrink size during fragile recoveries to avoid
churning the account, then expand size only when recovery is stable.

This module does that detection and emits a single multiplier the
adaptive-risk pipeline applies on top of the buffer-driven sizing.

Three classifications
---------------------
  NORMAL          — equity stable or trending nicely. factor = 1.0
  FRAGILE_RECOVERY — equity oscillates in DD without making new highs
                     across a window. factor = 0.5 (halve adaptive risk)
  STABLE_PEAK     — equity has been above baseline for N hours
                     without dipping below baseline. factor = 1.5
                     (within max_risk_pct ceiling)

Storage
-------
SQLite table `equity_samples` in v2.db, atomic appends per tick. Keep
last 2000 samples per account (rolling). At 5s polling that's ~3 hours
of samples — enough to detect intraday oscillations. For longer-window
detection (e.g. fragile-recovery over a day), the math here uses bar
counts, not wall-clock — works regardless of poll cadence.

Pure-ish module — has SQLite I/O but no globals or threading.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


class EquityRegime(str, Enum):
    NORMAL = "normal"
    FRAGILE_RECOVERY = "fragile_recovery"
    STABLE_PEAK = "stable_peak"


@dataclass(frozen=True)
class RegimeAssessment:
    """Output of `classify_regime`. Caller uses `recovery_factor`
    as the multiplier into adaptive_risk."""
    regime: EquityRegime
    recovery_factor: float
    reason: str
    n_samples: int
    current_equity: float
    baseline_equity: float
    rolling_high: float
    rolling_low: float


_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS equity_samples (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        sampled_at_utc TEXT NOT NULL,
        equity       REAL NOT NULL,
        balance      REAL,
        open_pnl     REAL
    )
"""

_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_equity_samples_time
        ON equity_samples (sampled_at_utc DESC)
"""

# Keep this many rows. Older rows trimmed at sample time.
_MAX_SAMPLES = 2000


def _ensure_table(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as c:
        c.execute(_TABLE_DDL)
        c.execute(_INDEX_DDL)


def record_sample(
    db_path: Path | str,
    *,
    equity: float,
    balance: Optional[float] = None,
    open_pnl: Optional[float] = None,
    now_utc: Optional[datetime] = None,
) -> None:
    """Append one equity sample. Trims oldest rows beyond _MAX_SAMPLES."""
    db_path = Path(db_path)
    when = (now_utc or datetime.now(timezone.utc)).isoformat()
    try:
        _ensure_table(db_path)
        with sqlite3.connect(str(db_path)) as c:
            c.execute(
                "INSERT INTO equity_samples "
                "(sampled_at_utc, equity, balance, open_pnl) "
                "VALUES (?, ?, ?, ?)",
                (when, float(equity),
                 (float(balance) if balance is not None else None),
                 (float(open_pnl) if open_pnl is not None else None)),
            )
            # Trim to last _MAX_SAMPLES rows
            c.execute(
                "DELETE FROM equity_samples "
                "WHERE id NOT IN ("
                "  SELECT id FROM equity_samples "
                "  ORDER BY id DESC LIMIT ?)",
                (_MAX_SAMPLES,),
            )
    except Exception as e:
        log.warning("equity_tracker.record_sample failed (%s)", e)


def load_recent_samples(
    db_path: Path | str, *, limit: int = 1000,
) -> list[float]:
    """Return the last `limit` equity values, oldest-first."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    try:
        _ensure_table(db_path)
        with sqlite3.connect(str(db_path)) as c:
            rows = c.execute(
                "SELECT equity FROM equity_samples "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [float(r[0]) for r in reversed(rows)]
    except Exception as e:
        log.warning("equity_tracker.load_recent_samples failed (%s)", e)
        return []


def classify_regime(
    samples: list[float],
    *,
    baseline_equity: float,
    fragile_lookback: int = 200,         # ~17 min at 5s polling
    fragile_min_oscillations: int = 2,
    stable_peak_above_baseline_pct: float = 1.0,
    stable_peak_min_samples_above: int = 720,   # ~1h at 5s polling
) -> RegimeAssessment:
    """Classify the current equity regime from a sample window.

    `fragile_recovery`: in the last `fragile_lookback` samples, equity
    crossed BELOW baseline ≥ `fragile_min_oscillations` times. That's
    the 91k → 93k → 91k pattern — bouncing back up but not staying
    above baseline.

    `stable_peak`: every sample in the last `stable_peak_min_samples_above`
    is ≥ baseline × (1 + stable_peak_above_baseline_pct/100). Means
    we've been comfortably above baseline for an extended window —
    safe to lean into upside.

    Otherwise `normal`.
    """
    n = len(samples)
    if n == 0:
        return RegimeAssessment(
            regime=EquityRegime.NORMAL,
            recovery_factor=1.0,
            reason="no samples yet",
            n_samples=0,
            current_equity=0.0,
            baseline_equity=baseline_equity,
            rolling_high=0.0,
            rolling_low=0.0,
        )
    current = samples[-1]
    rolling_high = max(samples)
    rolling_low = min(samples)

    # ── STABLE_PEAK check ────────────────────────────────────────────
    peak_threshold = baseline_equity * (
        1.0 + stable_peak_above_baseline_pct / 100.0
    )
    window_for_peak = samples[-stable_peak_min_samples_above:]
    if (len(window_for_peak) >= stable_peak_min_samples_above
            and all(s >= peak_threshold for s in window_for_peak)):
        return RegimeAssessment(
            regime=EquityRegime.STABLE_PEAK,
            recovery_factor=1.5,
            reason=(
                f"stable above baseline+{stable_peak_above_baseline_pct}%"
                f" for {len(window_for_peak)} samples"
            ),
            n_samples=n, current_equity=current,
            baseline_equity=baseline_equity,
            rolling_high=rolling_high, rolling_low=rolling_low,
        )

    # ── FRAGILE_RECOVERY check ───────────────────────────────────────
    # Count baseline crossings in the recent window. A "down crossing"
    # is when sample[i-1] >= baseline and sample[i] < baseline. Two or
    # more = oscillating around baseline → fragile.
    recent = samples[-fragile_lookback:]
    n_down_crossings = 0
    for i in range(1, len(recent)):
        if recent[i - 1] >= baseline_equity and recent[i] < baseline_equity:
            n_down_crossings += 1
    if (n_down_crossings >= fragile_min_oscillations
            and current < baseline_equity):
        return RegimeAssessment(
            regime=EquityRegime.FRAGILE_RECOVERY,
            recovery_factor=0.5,
            reason=(
                f"{n_down_crossings} down-crossings of baseline in last "
                f"{len(recent)} samples; currently below baseline"
            ),
            n_samples=n, current_equity=current,
            baseline_equity=baseline_equity,
            rolling_high=rolling_high, rolling_low=rolling_low,
        )

    # ── Default: normal ─────────────────────────────────────────────
    return RegimeAssessment(
        regime=EquityRegime.NORMAL,
        recovery_factor=1.0,
        reason="no fragile/peak pattern detected",
        n_samples=n, current_equity=current,
        baseline_equity=baseline_equity,
        rolling_high=rolling_high, rolling_low=rolling_low,
    )


def assess_now(
    db_path: Path | str,
    *,
    baseline_equity: float,
    lookback: int = 1000,
) -> RegimeAssessment:
    """Convenience: load recent samples + classify in one call."""
    samples = load_recent_samples(db_path, limit=lookback)
    return classify_regime(samples, baseline_equity=baseline_equity)
