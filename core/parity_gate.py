"""
parity_gate.py — wraps storage.parity_log to expose:
  - record_pass(strategy, divergence_dollars, at_utc=None)
  - last_pass_for(strategy_name) -> Optional[(datetime, divergence)]
  - is_recent(strategy_name, max_age_hours=24) -> bool

The replay-parity test (tests/test_replay_parity.py) calls record_pass on
green so live_executor's pre-flight check #5 can confirm parity is fresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import storage


class ParityGate:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        storage.init_schema(self.db_path)

    def record_pass(self, strategy: str, divergence_dollars: float,
                    at_utc: datetime | None = None) -> None:
        ts = (at_utc or datetime.now(timezone.utc)).isoformat()
        storage.record_parity_pass(self.db_path, strategy, ts, divergence_dollars)

    def last_pass_for(self, strategy: str) -> tuple[datetime, float] | None:
        row = storage.last_parity_pass(self.db_path, strategy)
        if row is None:
            return None
        ts_str, div = row
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (ts, div)

    def is_recent(self, strategy: str, *,
                  max_age_hours: float = 24.0,
                  now_utc: datetime | None = None) -> bool:
        last = self.last_pass_for(strategy)
        if last is None:
            return False
        ts, _ = last
        now = now_utc or datetime.now(timezone.utc)
        return (now - ts) <= timedelta(hours=max_age_hours)
