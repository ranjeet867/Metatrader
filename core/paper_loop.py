"""
paper_loop.py — orchestrates many (strategy, ticker, tf, params, lots)
configurations, ticking each via core.runner.tick on every poll.

Architecture:
  - PaperLoop holds a list of PaperStrategyConfig.
  - Each tick(): for each config, fetch the latest view (via the candle
    fetcher), call runner.tick, persist any closes to data/v2.db, update
    heartbeat. Bridge errors are caught and logged — the loop never crashes.
  - start()/stop() spawn a daemon thread that calls tick() every poll_seconds.
  - heartbeat written to paper_runs.heartbeat_at_utc EVERY tick (even when
    no bars arrive) so the dashboard can see the loop is alive.

Note: this module never SENDS orders. Paper trading by definition is
in-memory + DB-only. The PaperExecutor's closed_trades are persisted with
mode='paper'.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from core import storage
from core.paper_executor import PaperExecutor
from core.runner import tick as runner_tick
from core.strategy import Strategy
from core.time_guards import TimeGuardCfg


@dataclass
class PaperStrategyConfig:
    """One slot in the paper portfolio."""
    strategy: Strategy
    symbol: str
    tf: str
    lots: float
    money_per_unit_price: float
    name_for_journal: str = ""

    def __post_init__(self):
        if not self.name_for_journal:
            self.name_for_journal = self.strategy.name


# A candle-fetcher: given (symbol, tf, n_bars) return a tz-aware DataFrame.
# The dashboard injects either a bridge fetcher or a parquet-cache reader.
CandleFetcherFn = Callable[[str, str, int], pd.DataFrame]


@dataclass
class TickEvent:
    """One row recorded in the loop's in-memory event queue (for the dashboard)."""
    occurred_at_utc: str
    config_name: str
    symbol: str
    tf: str
    kind: str           # "open" | "close" | "skipped" | "error" | "heartbeat"
    detail: str = ""


class PaperLoop:
    """Multi-strategy paper trading loop.

    Args:
        configs: list of PaperStrategyConfig (one per portfolio slot).
        candle_fetcher: function (symbol, tf, n_bars) → DataFrame.
        db_path: where to persist runs / trades / events.
        run_id: caller-supplied run id; auto-generated if None.
        time_guard_cfg: optional time-guard rules for both update_bar
                          forced-flats AND no-entry-window.
        commission_per_trade / slippage_per_fill_atr_frac: applied per
            executor (one executor per config).
        poll_seconds: interval between automatic ticks (when running via
            start()).
        max_history_bars: how many bars to fetch each tick.
    """

    def __init__(self, *, configs: list[PaperStrategyConfig],
                 candle_fetcher: CandleFetcherFn,
                 db_path: str | Path,
                 run_id: Optional[str] = None,
                 time_guard_cfg: Optional[TimeGuardCfg] = None,
                 commission_per_trade: float = 0.0,
                 slippage_per_fill_atr_frac: float = 0.0,
                 poll_seconds: float = 60.0,
                 max_history_bars: int = 500) -> None:
        self.configs = list(configs)
        self.candle_fetcher = candle_fetcher
        self.db_path = Path(db_path)
        self.run_id = run_id or f"PR_{uuid.uuid4().hex[:8]}"
        self.time_guard_cfg = time_guard_cfg
        self.commission_per_trade = float(commission_per_trade)
        self.slippage_per_fill_atr_frac = float(slippage_per_fill_atr_frac)
        self.poll_seconds = float(poll_seconds)
        self.max_history_bars = int(max_history_bars)

        storage.init_schema(self.db_path)

        # One executor per config — keeps positions per config isolated
        self._executors: dict[str, PaperExecutor] = {
            c.name_for_journal: PaperExecutor(
                max_open_positions=1,
                commission_per_trade=commission_per_trade,
                slippage_per_fill_atr_frac=slippage_per_fill_atr_frac,
                time_guard_cfg=time_guard_cfg,
                mode="paper",
            )
            for c in self.configs
        }
        # Last-known timestamp per config — only the FIRST appearance of a
        # new bar triggers a tick (avoid double-acting on the same closed bar)
        self._last_bar_seen: dict[str, pd.Timestamp] = {}

        # In-memory event log (for dashboard); also persisted on close
        self.events: list[TickEvent] = []
        self._lock = threading.Lock()

        # Threading
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status: str = "stopped"

        # Persist run row
        self._upsert_run(status="stopped")

    # --- properties --------------------------------------------------------

    @property
    def status(self) -> str:
        return self._status

    def get_executor(self, config_name: str) -> Optional[PaperExecutor]:
        return self._executors.get(config_name)

    def total_open(self) -> int:
        return sum(ex.n_open for ex in self._executors.values())

    def total_closed(self) -> int:
        return sum(len(ex.closed_trades) for ex in self._executors.values())

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._status = "running"
        self._upsert_run(status="running")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                          name=f"paper_loop:{self.run_id}")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._status = "stopped"
        self._upsert_run(status="stopped",
                          finished_at_utc=datetime.now(timezone.utc))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:    # pragma: no cover — never let the loop die
                self._record_event("error", "loop", "loop", "tick",
                                    f"{type(e).__name__}: {e}")
                self._status = "crashed"
                self._upsert_run(status="crashed")
            self._stop.wait(self.poll_seconds)

    # --- the actual tick ---------------------------------------------------

    def tick(self) -> list[TickEvent]:
        """Synchronous one-pass over every config. Returns events seen on
        this tick. Safe to call from a thread OR directly from a test."""
        now = datetime.now(timezone.utc)
        produced: list[TickEvent] = []

        for cfg in self.configs:
            ex = self._executors[cfg.name_for_journal]
            try:
                view = self.candle_fetcher(cfg.symbol, cfg.tf,
                                            self.max_history_bars)
            except Exception as e:
                ev = self._record_event("error", cfg.name_for_journal,
                                         cfg.symbol, cfg.tf,
                                         f"fetch failed: {type(e).__name__}: {e}")
                produced.append(ev)
                continue

            if view is None or view.empty:
                continue

            # Has a new closed bar arrived? Compare last_bar_seen.
            last_bar_t = pd.Timestamp(view["time"].iloc[-1])
            seen = self._last_bar_seen.get(cfg.name_for_journal)
            if seen is not None and last_bar_t == seen:
                # No new bar — only the heartbeat updates
                continue
            self._last_bar_seen[cfg.name_for_journal] = last_bar_t

            res = runner_tick(
                view, ex, cfg.strategy,
                symbol=cfg.symbol, tf=cfg.tf,
                money_per_unit_price=cfg.money_per_unit_price,
                lots=cfg.lots,
                time_guard_cfg=self.time_guard_cfg,
                compute_atr=self.slippage_per_fill_atr_frac > 0,
            )

            for pos in res.opens:
                produced.append(self._record_event(
                    "open", cfg.name_for_journal, cfg.symbol, cfg.tf,
                    f"{pos.direction} {pos.lots} @ {pos.actual_entry_price}",
                ))
            for closed in res.closes:
                self._persist_trade(cfg, closed, last_bar_t)
                produced.append(self._record_event(
                    "close", cfg.name_for_journal, cfg.symbol, cfg.tf,
                    f"{closed.close_reason} pnl=${closed.realized_pnl:+.2f}",
                ))
            if res.skipped_due_to_no_entry_window > 0:
                produced.append(self._record_event(
                    "skipped", cfg.name_for_journal, cfg.symbol, cfg.tf,
                    "no_entry_window",
                ))
            for err in res.errors:
                produced.append(self._record_event(
                    "error", cfg.name_for_journal, cfg.symbol, cfg.tf, err,
                ))

        # Heartbeat — always
        storage.heartbeat_paper_run(self.db_path, self.run_id, now.isoformat())
        return produced

    # --- helpers -----------------------------------------------------------

    def _persist_trade(self, cfg: PaperStrategyConfig, closed,
                        bar_time: pd.Timestamp) -> None:
        # Build a trade dict matching storage.save_trades's expected shape
        # The `run_id` is shared across all configs in this loop; trade_idx
        # is allocated by save_trades from len(rows). To avoid clashing on
        # PRIMARY KEY (run_id, trade_idx), we pass each closed trade
        # individually with a manually-chosen trade_idx.
        with storage.connect(self.db_path) as c:
            existing = c.execute(
                "SELECT COALESCE(MAX(trade_idx), -1) FROM trades WHERE run_id=?",
                (self.run_id,),
            ).fetchone()[0]
            trade_idx = (existing or -1) + 1
            c.execute(
                """
                INSERT OR REPLACE INTO trades (
                  run_id, trade_idx, symbol, direction, opened_at_utc,
                  closed_at_utc, entry_price, stop_price, target_price,
                  exit_price, lots, realized_pnl, r_multiple, close_reason,
                  mode, strategy, tf, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id, trade_idx, cfg.symbol, closed.direction,
                    pd.Timestamp(bar_time).isoformat(),
                    pd.Timestamp(bar_time).isoformat(),
                    closed.entry_price, closed.stop_price, closed.target_price,
                    closed.exit_price, closed.lots, closed.realized_pnl,
                    closed.r_multiple, closed.close_reason,
                    "paper", cfg.strategy.name, cfg.tf, None,
                ),
            )

    def _record_event(self, kind: str, config_name: str, symbol: str,
                       tf: str, detail: str) -> TickEvent:
        ev = TickEvent(
            occurred_at_utc=datetime.now(timezone.utc).isoformat(),
            config_name=config_name, symbol=symbol, tf=tf,
            kind=kind, detail=detail,
        )
        with self._lock:
            self.events.append(ev)
            # Cap the in-memory log to 1000 events
            if len(self.events) > 1000:
                self.events = self.events[-1000:]
        return ev

    def _upsert_run(self, *, status: str,
                     finished_at_utc: Optional[datetime] = None) -> None:
        cfg_summary = {
            "configs": [
                {"strategy": c.strategy.name, "symbol": c.symbol,
                 "tf": c.tf, "lots": c.lots}
                for c in self.configs
            ],
            "commission": self.commission_per_trade,
            "slippage_atr_frac": self.slippage_per_fill_atr_frac,
            "poll_seconds": self.poll_seconds,
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        storage.upsert_paper_run(
            self.db_path, self.run_id,
            started_at_utc=now_iso,
            status=status, config_json=json.dumps(cfg_summary),
            finished_at_utc=finished_at_utc.isoformat() if finished_at_utc else None,
            heartbeat_at_utc=now_iso,
        )
        # Also write a row in `runs` so the trades FK is satisfied. We use
        # the FIRST config's symbol/tf/strategy as the label; the full
        # multi-strategy detail lives in paper_runs.config_json.
        first = self.configs[0] if self.configs else None
        with storage.connect(self.db_path) as c:
            existing = c.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (self.run_id,),
            ).fetchone()
            if existing is None and first is not None:
                c.execute(
                    """
                    INSERT INTO runs
                      (run_id, started_at_utc, symbol, tf, strategy_name,
                       config_json, starting_balance)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (self.run_id, now_iso, first.symbol, first.tf,
                     f"PAPER:{first.strategy.name}",
                     json.dumps(cfg_summary), 0.0),
                )
