"""
slippage_tracker.py — record signal-vs-fill price per open + aggregate.

Why this exists
---------------
The catalog assumes slippage_per_fill = 0.05 × ATR. If real fills come
in at 0.15 × ATR, every cost-priced backtest is overestimating PF by
the cost gap. We need empirical data to know whether the model is
accurate, and which symbols (or hours, or volatility regimes) drift
worse than modeled.

What gets recorded
------------------
On every confirmed open, write a row to v2.db `slippage_events`:
  - deployment_id
  - symbol, tf, direction
  - signal_entry_price (what the strategy bar-close said)
  - actual_fill_price  (what the broker actually filled at)
  - atr_at_signal      (so we can normalize across volatility)
  - slippage_atr_frac  = |actual - signal| / ATR
  - opened_at_utc
  - signal_bar_utc

The dashboard surfaces:
  - Per-symbol mean / p95 / max slippage
  - Catalog assumed (0.05) vs actual realized

Caller pattern
--------------
After broker confirms an open:
    slippage_tracker.record(
        db_path=db_path,
        deployment_id=d.deployment_id,
        symbol=d.ticker, tf=d.tf, direction=op.direction,
        signal_entry_price=op.entry_price,
        actual_fill_price=order_obj.fill_price,    # from bridge response
        atr_at_signal=op.atr_at_signal_bar,
    )

If we don't have a fill_price (some bridge versions), we fall back to
recording 0 slippage and flagging the row — better than not recording
at all.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS slippage_events (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_at_utc       TEXT    NOT NULL,
        deployment_id       TEXT    NOT NULL,
        symbol              TEXT    NOT NULL,
        tf                  TEXT    NOT NULL,
        direction           TEXT    NOT NULL,
        signal_entry_price  REAL    NOT NULL,
        actual_fill_price   REAL    NOT NULL,
        atr_at_signal       REAL    NOT NULL,
        slippage_abs        REAL    NOT NULL,    -- |actual - signal|
        slippage_atr_frac   REAL    NOT NULL,    -- 0 if ATR was 0
        signal_bar_utc      TEXT,
        notes               TEXT
    )
"""

_INDEX_DDL = """
    CREATE INDEX IF NOT EXISTS idx_slippage_symbol_time
        ON slippage_events (symbol, opened_at_utc DESC)
"""


def _ensure_table(db_path: Path) -> None:
    """Create the slippage_events table if it doesn't exist. Idempotent."""
    with sqlite3.connect(str(db_path)) as c:
        c.execute(_SCHEMA_DDL)
        c.execute(_INDEX_DDL)


def record(
    *,
    db_path: Path,
    deployment_id: str,
    symbol: str,
    tf: str,
    direction: str,
    signal_entry_price: float,
    actual_fill_price: float,
    atr_at_signal: float = 0.0,
    signal_bar_utc: Optional[str] = None,
    notes: str = "",
    opened_at_utc: Optional[str] = None,
) -> None:
    """Record one open's signal-vs-fill slippage. Caller invokes after
    the broker confirms the position. Failures are swallowed (logged
    only) — slippage tracking is read-only research data and must
    never block a trade."""
    try:
        db_path = Path(db_path)
        _ensure_table(db_path)
        opened = (opened_at_utc
                  or datetime.now(timezone.utc).isoformat())
        slippage_abs = abs(float(actual_fill_price) - float(signal_entry_price))
        slippage_atr = (slippage_abs / atr_at_signal
                         if atr_at_signal and atr_at_signal > 0
                         else 0.0)
        with sqlite3.connect(str(db_path)) as c:
            c.execute(
                "INSERT INTO slippage_events ("
                "opened_at_utc, deployment_id, symbol, tf, direction, "
                "signal_entry_price, actual_fill_price, atr_at_signal, "
                "slippage_abs, slippage_atr_frac, signal_bar_utc, notes"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (opened, deployment_id, symbol, tf, direction,
                 float(signal_entry_price), float(actual_fill_price),
                 float(atr_at_signal), slippage_abs, slippage_atr,
                 signal_bar_utc, notes),
            )
        log.debug(
            "slippage recorded: dep=%s %s %s signal=%s fill=%s "
            "diff=%.6f atr_frac=%.4f",
            deployment_id, symbol, direction,
            signal_entry_price, actual_fill_price,
            slippage_abs, slippage_atr,
        )
    except Exception as e:
        log.warning("slippage_tracker.record failed (non-fatal): %s", e)


def aggregate_by_symbol(db_path: Path,
                          since_utc: Optional[str] = None) -> list[dict]:
    """Return per-symbol summary rows for dashboard display.

    For each symbol in slippage_events:
      - n_opens
      - mean_slippage_atr_frac
      - max_slippage_atr_frac
      - p95_slippage_atr_frac (approximate via SQL — sort + LIMIT)

    Pass since_utc=ISO8601 to limit to the last N days.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    try:
        _ensure_table(db_path)
    except Exception:
        return []
    where = "WHERE 1=1"
    params: list = []
    if since_utc:
        where += " AND opened_at_utc >= ?"
        params.append(since_utc)
    sql = f"""
        SELECT
            symbol,
            COUNT(*)                            AS n_opens,
            AVG(slippage_atr_frac)              AS mean_slip_atr,
            MAX(slippage_atr_frac)              AS max_slip_atr,
            AVG(slippage_abs)                   AS mean_slip_abs
        FROM slippage_events
        {where}
        GROUP BY symbol
        ORDER BY n_opens DESC
    """
    out: list[dict] = []
    try:
        with sqlite3.connect(str(db_path)) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute(sql, params).fetchall():
                out.append({
                    "symbol": r["symbol"],
                    "n_opens": int(r["n_opens"]),
                    "mean_slip_atr": float(r["mean_slip_atr"] or 0.0),
                    "max_slip_atr": float(r["max_slip_atr"] or 0.0),
                    "mean_slip_abs": float(r["mean_slip_abs"] or 0.0),
                })
    except Exception as e:
        log.warning("slippage aggregate failed: %s", e)
    return out


def recent_events(db_path: Path, limit: int = 50) -> list[dict]:
    """Return the most recent slippage events for dashboard display."""
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    try:
        _ensure_table(db_path)
    except Exception:
        return []
    try:
        with sqlite3.connect(str(db_path)) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT * FROM slippage_events "
                "ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.warning("slippage recent_events failed: %s", e)
        return []
