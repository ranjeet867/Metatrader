"""
journal.py — append-only audit trail for trades + signal-rejections + bridge
events + forced-flat events.

Every line that comes through here is a row in data/v2.db. Nothing in this
module mutates an existing row. The dashboard reads via separate queries
(no journal model writer, just storage helpers).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import storage


class JournalWriter:
    """Thin wrapper around storage.* writers — exists so the call sites use a
    consistent vocabulary (record_*) instead of mixing storage-level names."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        storage.init_schema(self.db_path)
        # Best-effort signal_rejections table — only write if it exists; we
        # didn't add it to the v2 schema since it overlaps bridge_events.

    # --- trades ---

    def record_trade(self, trade: dict[str, Any], *, run_id: str) -> None:
        """Persist a single ClosedTrade-shaped dict. The caller controls run_id
        (one paper-loop run / live-run / backtest run aggregates many trades)."""
        storage.save_trades(self.db_path, run_id, [trade])

    # --- bridge ---

    def record_bridge_event(self, *, method: str, latency_ms: int, ok: bool,
                             error: str | None = None,
                             at_utc: datetime | None = None) -> None:
        ts = (at_utc or datetime.now(timezone.utc)).isoformat()
        storage.record_bridge_event(self.db_path, ts, method, ok, latency_ms,
                                     error)

    # --- forced-flat events ---

    def record_forced_flat(self, *, symbol: str, mode: str, reason: str,
                            mark_price: float, pnl_at_close: float,
                            occurred_at_utc: datetime | None = None) -> None:
        ts = (occurred_at_utc or datetime.now(timezone.utc)).isoformat()
        storage.record_forced_flat(self.db_path, ts, symbol, mode, reason,
                                    mark_price, pnl_at_close)

    # --- runs ---

    def upsert_paper_run(self, run_id: str, *, status: str, config_json: str,
                         started_at_utc: datetime | None = None,
                         finished_at_utc: datetime | None = None,
                         heartbeat_at_utc: datetime | None = None) -> None:
        storage.upsert_paper_run(
            self.db_path, run_id,
            started_at_utc=(started_at_utc or datetime.now(timezone.utc)).isoformat(),
            status=status, config_json=config_json,
            finished_at_utc=finished_at_utc.isoformat() if finished_at_utc else None,
            heartbeat_at_utc=heartbeat_at_utc.isoformat() if heartbeat_at_utc else None,
        )
