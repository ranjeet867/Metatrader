"""
experiment_log.py — persistent JSON store for backtest experiments.

Why this exists
---------------
Running variant sweeps repeatedly without remembering which combos
were tested is wasteful. This module persists every backtest result
under a stable key, so subsequent runs can:
  - Skip already-tested combos (idempotency)
  - Show "what works" + "what doesn't" across the full history
  - Build a leaderboard across strategies

Storage
-------
data/experiment_log.json — one big dict, keyed by experiment_id.
Each entry stores: timestamp, variant params, ticker, tf, all stats.

Caller pattern
--------------
    from core import experiment_log

    log = experiment_log.load()
    if not log.has(strategy="ema9_trail", variant="V4",
                       ticker="XAUUSD", tf="D1"):
        stats = run_backtest(...)
        log.record("ema9_trail", "V4", "XAUUSD", "D1",
                       stats=stats, params={"use_50ema": True})
        log.save()

    # Query leaderboard
    deploy_safe = log.deploy_safe_cells()
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOG_PATH = Path("data") / "experiment_log.json"


@dataclass
class ExperimentRecord:
    """One backtest result. Self-describing so the JSON is readable."""
    experiment_id: str         # "ema9_trail_V4_XAUUSD_D1"
    strategy: str              # "ema9_trail"
    variant: str               # "V4"
    ticker: str
    tf: str
    recorded_at_utc: str
    params: dict[str, Any]     # variant config
    stats: dict[str, Any]      # PF, WR, n, mean_R, DD%, etc.

    @property
    def deploy_safe(self) -> bool:
        s = self.stats
        return (
            int(s.get("n", 0)) >= 15
            and float(s.get("pf", 0)) >= 1.05
            and float(s.get("mean_R", 0)) > 0
            and float(s.get("max_dd_pct", 100)) <= 30
        )


@dataclass
class ExperimentLog:
    path: Path
    records: dict[str, ExperimentRecord] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = DEFAULT_LOG_PATH) -> "ExperimentLog":
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls(path=path)
        records = {}
        for k, v in data.get("records", {}).items():
            try:
                records[k] = ExperimentRecord(**v)
            except Exception:
                continue
        return cls(path=path, records=records)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            "n_records": len(self.records),
            "records": {k: vars(v) for k, v in self.records.items()},
        }
        # Atomic write
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(self.path)

    @staticmethod
    def make_id(strategy: str, variant: str, ticker: str, tf: str) -> str:
        # Normalize so naming is consistent
        return f"{strategy}__{variant}__{ticker}__{tf}".replace(".", "_")

    def has(self, *, strategy: str, variant: str, ticker: str, tf: str,
              not_older_than_days: float | None = None) -> bool:
        eid = self.make_id(strategy, variant, ticker, tf)
        rec = self.records.get(eid)
        if rec is None:
            return False
        if not_older_than_days is not None:
            try:
                ts = datetime.fromisoformat(rec.recorded_at_utc)
                age_d = (datetime.now(timezone.utc) - ts).total_seconds() / 86400
                if age_d > not_older_than_days:
                    return False
            except (ValueError, TypeError):
                return False
        return True

    def record(self, strategy: str, variant: str, ticker: str, tf: str,
                  *, stats: dict, params: dict | None = None) -> ExperimentRecord:
        eid = self.make_id(strategy, variant, ticker, tf)
        rec = ExperimentRecord(
            experiment_id=eid,
            strategy=strategy, variant=variant,
            ticker=ticker, tf=tf,
            recorded_at_utc=datetime.now(timezone.utc).isoformat(),
            params=dict(params or {}),
            stats=dict(stats),
        )
        self.records[eid] = rec
        return rec

    def get(self, *, strategy: str, variant: str, ticker: str,
              tf: str) -> ExperimentRecord | None:
        return self.records.get(self.make_id(strategy, variant, ticker, tf))

    def by_strategy(self, strategy: str) -> list[ExperimentRecord]:
        return [r for r in self.records.values() if r.strategy == strategy]

    def deploy_safe_cells(self) -> list[ExperimentRecord]:
        """All records passing deploy gates, sorted by PF desc."""
        safe = [r for r in self.records.values() if r.deploy_safe]
        safe.sort(key=lambda r: -float(r.stats.get("pf", 0)))
        return safe

    def leaderboard(self, top_n: int = 20) -> list[ExperimentRecord]:
        """Top N by mean_R × PF (combined score proxy)."""
        scored = []
        for r in self.records.values():
            pf = float(r.stats.get("pf", 0))
            mean_R = float(r.stats.get("mean_R", 0))
            n = int(r.stats.get("n", 0))
            if n < 15 or pf <= 0:
                continue
            # Simple ranking score: pf × mean_R × log(n) — favours
            # consistent, profitable, well-sampled cells
            import math
            score = pf * max(mean_R, 0.01) * math.log(max(n, 2))
            scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _, r in scored[:top_n]]


def load(path: Path = DEFAULT_LOG_PATH) -> ExperimentLog:
    """Convenience top-level loader."""
    return ExperimentLog.load(path)
