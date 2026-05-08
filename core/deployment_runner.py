"""
deployment_runner.py — the LIVE loop that was missing.

Scope:
  Read deployments.json, for each deployment with status=='paper' or 'live',
  on a poll interval:
    1. Fetch latest bars from MT5 bridge (or any candle fetcher)
    2. Call core.runner.tick(view, executor, ...)
    3. Open positions go through PaperExecutor (paper) or via the bridge
       (live) — the latter is the actual order_send round-trip
    4. Persist trades + heartbeat to data/v2.db

  Designed to run either:
    • As a daemon thread inside the dashboard (`start()` / `stop()`),
    • Or as a standalone CLI script (`scripts/run_deployments.py`).

  Each deployment gets its own PaperExecutor (paper) or shared
  LiveExecutor (live). The LiveExecutor delegates to the bridge.

  WHY THIS EXISTS: previously, deployments marked "live" in the UI did
  nothing because no module polled them. This module is the missing link
  between the UI deployment metadata and actual broker order submission.

  INVARIANTS:
    • Replay-parity gate must be fresh (<24h) before live deployment
      can fire — checked on each tick. If parity expires mid-session,
      the deployment is auto-demoted to paper for safety (configurable).
    • Circuit breaker is consulted on every tick. HALT auto-pauses every
      live deployment on the account (via core.circuit_breaker.enforce).
    • Position guard checks every signal against the account's current
      open positions to refuse double-exposure.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from core import (
    account_manager, circuit_breaker, deployment as dep_mod,
    parity_gate as parity_gate_mod, position_guard, storage,
)
from core.live_executor import (
    BridgeOrderRejected, LiveExecutor,
)
from core.paper_executor import PaperExecutor
from core.runner import tick as runner_tick

log = logging.getLogger(__name__)


CandleFetcherFn = Callable[[str, str, int], pd.DataFrame]
"""Callable: (symbol, tf, n_bars) → DataFrame with time/open/high/low/close columns."""


@dataclass
class TickRecord:
    """One observation from a single tick — for the dashboard heartbeat."""
    deployment_id: str
    occurred_at_utc: str
    n_opens: int = 0
    n_closes: int = 0
    n_skipped: int = 0
    error: str = ""
    skip_reason: str = ""    # "no_new_bar" | "already_holding" | "circuit_breaker" | ...


@dataclass
class DeploymentRunner:
    """The thing that actually drives deployments on a schedule.

    Construct one per account login. Call start() to spawn the daemon
    thread; stop() to halt cleanly. Tick events are appended to
    `recent_events` (capped) so the dashboard can show heartbeat.
    """
    login: int
    candle_fetcher: CandleFetcherFn
    bridge_call: Optional[Callable] = None     # required for live mode
    db_path: Optional[Path] = None
    poll_seconds: float = 60.0
    max_history_bars: int = 500
    max_event_log: int = 200
    # User feedback (Phase 30c): auto-demoting LIVE → PAPER on parity
    # expiry was bad behaviour.
    #
    # User feedback (Phase 30d): blocking new opens on parity expiry is
    # ALSO bad — once you've moved a strategy to live, it should fire
    # opens until you explicitly disable / delete it. Vacation use:
    # leave the runner running for weeks without intervention.
    #
    # New defaults:
    #   enforce_parity_freshness=False  → parity is a deploy-time gate,
    #     not an ongoing 24h check. Set to True (opt-in) if you want
    #     the strict behaviour back.
    #   auto_demote_on_parity_expiry=False  → never auto-changes status.
    auto_demote_on_parity_expiry: bool = False
    enforce_parity_freshness: bool = False
    position_guard_policy: str = "strict"
    # Edge-decay autolearner — auto-demote LIVE→PAPER when rolling
    # live R has been > 2σ below catalog R for ≥ MIN_RED_TRADES (15)
    # consecutive trades. Defaults to ON because it's a defensive
    # gate; opt-out by passing enforce_edge_decay_demote=False.
    enforce_edge_decay_demote: bool = True
    # News calendar blackout — refuse to open new positions within
    # ±15min of a high-impact red-folder event affecting this ticker.
    # Cache file lives in data/news_calendar.json; refreshed daily by
    # `make refresh-news-calendar` or any first-call to load_cached().
    enforce_news_blackout: bool = True
    news_blackout_window_minutes: int = 15
    # Correlation-cluster concurrent-position cap — refuse to open a
    # position when the symbol's cluster (e.g. metals_long, us_indices)
    # already has `correlation_cluster_cap` positions live across all
    # deployments. Independent of position_guard which only sees
    # same-symbol collisions; this catches cross-symbol concentration
    # (4 metals all running at once = 4× the dollar risk on USD weakness).
    enforce_correlation_cluster_cap: bool = True
    correlation_cluster_cap: int = 2

    # Internal
    _executors_paper: dict = field(default_factory=dict)
    _live_executor: Optional[LiveExecutor] = None
    _strategies_cache: dict = field(default_factory=dict)
    _last_seen_bar: dict = field(default_factory=dict)
    _running: bool = False
    _thread: Optional[threading.Thread] = None
    _recent_events: list = field(default_factory=list)
    _last_tick_at_utc: Optional[str] = None
    _heartbeat_at_utc: Optional[str] = None
    # Last forensic-snapshot capture timestamp. Snapshots fire at most
    # once every 60s to keep table growth bounded (~525k rows/year).
    _last_forensic_capture_utc: Optional[str] = None
    # Set of deployment_ids whose freshness fields
    # (last_evaluated_at_utc / last_signal_at_utc) changed during the
    # current tick. Flushed to disk by tick_once() at the end of the
    # loop — single write per cycle even if multiple deployments
    # ticked. Pre-fix the runner never wrote these fields → user saw
    # `null` after 16h live and reasonably thought the runner was dead.
    _dep_dirty: set = field(default_factory=set)
    # Per-tick (ticker, tf) → DataFrame cache. Two deployments on the
    # same EURUSD M15 should share a single bridge fetch + a single
    # parquet write per tick. Cleared at the start of every tick_once.
    _tick_data_cache: dict = field(default_factory=dict)
    # Phase-32 #6: bridge consecutive-failure counter. Each unsuccessful
    # candle fetch / order send increments this; each successful one
    # resets it. After N=3 consecutive failures we emit a CRITICAL log
    # line and a `bridge_alarm` row in bridge_events. The dashboard
    # surfaces this as a red banner on Command Center.
    _bridge_consecutive_failures: int = 0
    _bridge_alarm_threshold: int = 3
    _bridge_alarm_active: bool = False
    # Per-deployment trade-persistence state (Bug B fix, 2026-05-08).
    # _runs_seeded — deployment_ids whose parent `runs` row we've created
    #   in this DB. Required because trades.run_id has a FK on runs.run_id
    #   and PRAGMA foreign_keys=ON is set on every connection. Without
    #   the parent row, every INSERT into trades fails silently and the
    #   circuit breaker (which reads `trades` for realised PnL / streak
    #   / daily loss) becomes blind to actual losses. We seed lazily on
    #   first persist and remember in-process so we don't re-INSERT
    #   every close.
    # _trade_idx_by_dep — monotonic per-deployment counter for
    #   trades.trade_idx. Pre-fix the runner hardcoded 0, causing every
    #   close after the first per deployment to violate the
    #   PRIMARY KEY (run_id, trade_idx). On startup we hydrate from
    #   `SELECT MAX(trade_idx)+1 FROM trades WHERE run_id=?` so a
    #   restart never collides with previously persisted trades.
    _runs_seeded: set = field(default_factory=set)
    _trade_idx_by_dep: dict = field(default_factory=dict)
    # Bug E state — throttle account-mismatch alarms to once per minute
    # so a bad MT5 state doesn't fill bridge_events with thousands of
    # identical rows on the 5-second tick cadence.
    _last_account_mismatch_alarm_at: Optional[str] = None
    # Bug C state — open live tickets being tracked for close-reconciliation.
    # Map: ticket(int) → LiveOpenContext-as-dict. Populated on successful
    # broker open in _mirror_opens_to_broker; consumed by
    # _reconcile_live_closes when the ticket vanishes from the broker.
    # Persisted to runner_state.json so a runner restart doesn't drop
    # context on still-open positions.
    _open_live_tickets: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.db_path is None:
            self.db_path = account_manager.get_db_path(self.login)
        self.db_path = Path(self.db_path)
        storage.init_schema(self.db_path)
        # Restart-survivability: load the last-seen-bar timestamps from
        # disk so a Ctrl+C+restart doesn't re-process bars that were
        # already handled. Without this, after a restart the runner
        # could fire duplicate signals on bars the previous run had
        # already acted on.
        self._load_state()

    # ── Persistent state (survives Ctrl+C + restart) ────────────────
    @property
    def _state_path(self) -> Path:
        return self.db_path.parent / "runner_state.json"

    def _load_state(self) -> None:
        """Read the persisted last-seen-bar map. Silent on first run."""
        import json
        p = self._state_path
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("runner_state.json corrupt — starting fresh")
            return
        last_seen_raw = data.get("last_seen_bar", {})
        for dep_id, ts_str in last_seen_raw.items():
            try:
                self._last_seen_bar[dep_id] = pd.Timestamp(ts_str)
            except Exception:
                continue
        if self._last_seen_bar:
            log.info("Restored last-seen bars for %d deployment(s) "
                      "from %s", len(self._last_seen_bar), p.name)
        # Bug C: restore the open-live-tickets map so a runner restart
        # doesn't lose context on positions still open at the broker.
        # Keys are JSON strings, convert back to ints.
        open_live_raw = data.get("open_live_tickets", {})
        for ticket_str, ctx_dict in open_live_raw.items():
            try:
                self._open_live_tickets[int(ticket_str)] = ctx_dict
            except Exception:
                continue
        if self._open_live_tickets:
            log.info("Restored %d open live ticket(s) for "
                      "close-reconciliation",
                      len(self._open_live_tickets))

    def _save_state(self) -> None:
        """Write the current last-seen-bar map. Called every tick + on
        clean shutdown."""
        import json
        p = self._state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = {
                "last_seen_bar": {
                    dep_id: ts.isoformat() if hasattr(ts, "isoformat")
                              else str(ts)
                    for dep_id, ts in self._last_seen_bar.items()
                },
                # Bug C: persist open-live-tickets map so restart-survivability
                # extends to in-flight broker positions. Keys are stringified
                # for JSON; restored to int on _load_state.
                "open_live_tickets": {
                    str(t): ctx for t, ctx in self._open_live_tickets.items()
                },
                "saved_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(p)   # atomic
        except OSError as e:
            log.warning("could not save runner_state.json: %s", e)

    # ---- Startup integrity check (#11) ─────────────────────────────────
    def assert_deployment_params_match_catalog(self) -> list[str]:
        """For each LIVE deployment, instantiate the strategy via the
        same code path the runner uses on every tick, then assert the
        resulting params dict matches the catalog row's
        source_config_json. Returns a list of mismatch warnings (empty
        if all match).

        Pre-fix: the runner could silently use default params for a
        non-default variant (e.g. `rsi_25_75` → uses 30/70 defaults).
        Live behavior would diverge from the backtest the user clicked
        Deploy on, with no error. Now: this runs at start() and on
        Command Center's "Validate" button. Mismatches are loud.

        Note: this does NOT fail-stop the runner — that would be too
        aggressive (could lock the user out during a legit migration).
        Instead it logs CRITICAL and surfaces via the dashboard.
        """
        from core import edge_catalog as _ec
        from core import deployment as _dep_mod
        warnings: list[str] = []
        try:
            deps = _dep_mod.load_deployments(self.login)
        except Exception as e:
            warnings.append(f"could not load deployments: {e}")
            return warnings
        for d in deps:
            if d.status != "live":
                continue
            try:
                strat = self._get_strategy(d)
            except Exception as e:
                warnings.append(
                    f"{d.deployment_id}: strategy build failed: {e}"
                )
                continue
            if strat is None:
                warnings.append(
                    f"{d.deployment_id}: strategy class not registered"
                )
                continue
            edge_row = _ec.best_for(d.ticker, d.tf, d.strategy)
            if edge_row is None:
                continue   # No catalog entry — nothing to assert against
            cfg_json = getattr(edge_row, "source_config_json", "") or ""
            if not cfg_json:
                continue   # Markdown-only row, no params to compare
            try:
                import json as _json
                catalog_params = _json.loads(cfg_json)
            except (ValueError, TypeError):
                continue
            # Walk the catalog params and confirm each one is reflected
            # on the strategy instance. We only check fields the strategy
            # actually has — extras in catalog (like `long_only` that
            # lives on the deployment, not the strategy params) are ok.
            actual_params = getattr(strat, "params", None)
            if actual_params is None:
                continue
            mismatches: list[str] = []
            for k, v in catalog_params.items():
                if k == "long_only":
                    continue   # deployment-level, not strategy params
                if not hasattr(actual_params, k):
                    continue
                got = getattr(actual_params, k)
                if got != v:
                    mismatches.append(f"{k}: catalog={v} live={got}")
            if mismatches:
                msg = (f"{d.deployment_id}: live strategy params DIVERGE "
                       f"from catalog source_config_json — "
                       + ", ".join(mismatches))
                log.critical(msg)
                warnings.append(msg)
        return warnings

    # ---- Public lifecycle ----
    def start(self) -> None:
        if self._running:
            return
        # Run the params-vs-catalog assertion BEFORE going live. We
        # still start the runner even if there are mismatches (warnings
        # only), but the user sees them in the log + can fail-stop via
        # the Command Center.
        try:
            warnings = self.assert_deployment_params_match_catalog()
            for w in warnings:
                log.warning("deployment-params-mismatch: %s", w)
            if not warnings:
                log.info("deployment-params-check: all live deployments "
                         "match catalog source_config_json")
        except Exception:
            log.exception("deployment-params-check failed (non-fatal)")
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"deployment-runner-{self.login}",
        )
        self._thread.start()
        log.info("DeploymentRunner started for login=%s", self.login)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=self.poll_seconds + 5)
        # Final state save on clean shutdown — Ctrl+C lands here.
        try:
            self._save_state()
        except Exception:
            pass
        log.info("DeploymentRunner stopped for login=%s "
                  "(state saved to %s)", self.login,
                  self._state_path.name)

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ---- Single tick (testable in isolation) ----
    def tick_once(self) -> list[TickRecord]:
        """Run one polling cycle across all active deployments. Returns
        a TickRecord per deployment for observability. Catches every
        exception per-deployment so one bad strategy never poisons the
        loop."""
        records: list[TickRecord] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Load deployments fresh each tick — UI changes apply on next loop
        try:
            deployments = dep_mod.load_deployments(self.login)
        except Exception as e:
            log.exception("load_deployments failed: %s", e)
            return records

        active = [d for d in deployments if d.status in ("paper", "live")]
        if not active:
            # No deployments active right now — but the runner IS alive.
            # Update heartbeat and emit a runner_heartbeat bridge_event
            # so health checks can prove the loop is ticking. Without
            # this, a user who has all-idle deployments looks identical
            # (to the health check) to a hung runner.
            self._heartbeat_at_utc = now_iso
            try:
                self._maybe_record_heartbeat_event(now_iso, n_active=0)
            except Exception:
                log.debug("heartbeat (idle path) failed", exc_info=True)
            return records

        # 2. Circuit-breaker pre-check (per-account)
        try:
            cb_cfg = circuit_breaker.load_config(self.login)
            # Count current open positions across deployments via bridge
            n_open = self._count_open_positions()
            cb_status = circuit_breaker.evaluate(
                self.db_path, mode="live", cfg=cb_cfg,
                open_positions_count=n_open,
            )
        except Exception:
            log.exception("circuit_breaker.evaluate failed")
            cb_status = None

        if cb_status is not None and cb_status.state == "HALT":
            try:
                circuit_breaker.enforce(self.login, cb_status)
            except Exception:
                log.exception("circuit_breaker.enforce failed")
            for d in active:
                records.append(TickRecord(
                    deployment_id=d.deployment_id,
                    occurred_at_utc=now_iso,
                    error=f"CIRCUIT_HALT: {' | '.join(cb_status.reasons)}",
                ))
            self._heartbeat_at_utc = now_iso
            self._record_events(records)
            return records

        block_opens_reason = (
            "; ".join(cb_status.reasons)
            if cb_status is not None and cb_status.state == "STOP_NEW"
            else None
        )

        # 2b. Bug E: account-mismatch safety guard.
        # The Wine MT5 bridge serves whatever account is currently
        # logged into MT5. If the user switches accounts in MT5
        # (File → Login → other FTMO account) WITHOUT updating our
        # account_manager config, the bridge silently starts routing
        # `order_send` / `positions_get` / `history_deals_get` to the
        # OTHER login while we keep persisting trades into THIS
        # login's per-account DB.
        # Result: orders fire on the wrong account, attributed to the
        # configured login, with the wrong circuit_breaker watching.
        # We REFUSE to do any bridge work this tick if the live login
        # doesn't match self.login. Setting bridge_alarm + HALT until
        # the user resolves it (either re-login MT5 to this account, or
        # add the new account to account_manager and start a separate
        # runner process for it).
        try:
            account_match = self._assert_account_match()
        except Exception:
            log.exception("account-match check raised — failing CLOSED "
                          "(treating as mismatch) for safety")
            account_match = False
        if not account_match:
            for d in active:
                records.append(TickRecord(
                    deployment_id=d.deployment_id,
                    occurred_at_utc=now_iso,
                    error="ACCOUNT MISMATCH — bridge serving a different "
                          "login than this runner is configured for",
                ))
            self._heartbeat_at_utc = now_iso
            self._record_events(records)
            return records

        # 3. Build current open-positions snapshot for the position guard
        open_positions_snapshot = self._open_positions_snapshot(deployments)

        # 3a. Bug C — reconcile any live tickets that vanished since last
        # tick (server-side TP/SL/manual closes). Without this, the bot's
        # closed live trades never reach the `trades` table and downstream
        # safety nets (circuit_breaker, edge_decay) stay blind.
        try:
            self._reconcile_live_closes(deployments)
        except Exception:
            log.exception("live close reconciliation failed (non-fatal)")

        # 3b. Weekend / daily forced-flat — pre-fix the live runner had
        # NO weekend-flat enforcement. backtest + paper close positions
        # at Friday 19:55 UTC; LIVE didn't, so a Friday-afternoon position
        # would sit through the weekend exposing the user to gap risk.
        # Now we check the time-guard cfg every tick and force-close
        # any open broker positions when the close-window fires.
        try:
            self._force_flat_if_close_window(now_iso, deployments)
        except Exception:
            log.exception("force-flat check failed")

        # Clear per-tick (ticker, tf) → DataFrame cache so each cycle
        # starts fresh. Without this, two deployments on the same
        # (ticker, tf) would share the SAME stale DataFrame across
        # multiple tick cycles, defeating the live-data update.
        self._tick_data_cache.clear()

        # 4. Tick each active deployment
        for d in active:
            rec = TickRecord(deployment_id=d.deployment_id,
                              occurred_at_utc=now_iso)
            try:
                self._tick_deployment(d, rec,
                                         block_opens_reason=block_opens_reason,
                                         open_positions=open_positions_snapshot)
            except Exception as e:
                log.exception("tick %s failed", d.deployment_id)
                rec.error = f"{type(e).__name__}: {e}"
            records.append(rec)

        self._last_tick_at_utc = now_iso
        self._heartbeat_at_utc = now_iso

        # Forensic snapshot — append a system-state row at most every
        # 60s. Wrapped in try/except so a forensic-write failure NEVER
        # blocks the runner. Captures equity, position count, daily
        # P&L, regime — enough to reconstruct any anomaly's context
        # post-mortem.
        self._maybe_capture_forensic_snapshot(now_iso, deployments)

        self._record_events(records)
        # Persist last-seen-bar map every tick — cheap (≤1KB JSON, ≤12
        # entries) and means a Ctrl+C never loses progress.
        self._save_state()
        # Flush deployment freshness fields if any deployment's
        # last_evaluated_at_utc / last_signal_at_utc changed this tick.
        # We re-read deployments.json from disk to avoid stomping any
        # concurrent dashboard edits (status changes, notes, etc.) and
        # only mutate the freshness fields on the matching rows.
        # NOTE: dep_mod is the module-level import from line 46. We
        # MUST NOT do `from core import deployment as dep_mod` inside
        # this function — Python would treat that as a local-variable
        # assignment which shadows the module-level binding for the
        # ENTIRE function (including line 227's earlier use), breaking
        # tick_once with UnboundLocalError.
        if self._dep_dirty:
            try:
                disk = dep_mod.load_deployments(self.login)
                disk_by_id = {d.deployment_id: d for d in disk}
                in_mem_by_id = {d.deployment_id: d for d in deployments}
                changed_disk = False
                for dep_id in list(self._dep_dirty):
                    on_disk = disk_by_id.get(dep_id)
                    fresh = in_mem_by_id.get(dep_id)
                    if on_disk is None or fresh is None:
                        continue
                    if (on_disk.last_evaluated_at_utc
                            != fresh.last_evaluated_at_utc):
                        on_disk.last_evaluated_at_utc = (
                            fresh.last_evaluated_at_utc
                        )
                        changed_disk = True
                    if on_disk.last_signal_at_utc != fresh.last_signal_at_utc:
                        on_disk.last_signal_at_utc = fresh.last_signal_at_utc
                        changed_disk = True
                if changed_disk:
                    dep_mod.save_deployments(self.login, list(disk_by_id.values()))
                self._dep_dirty.clear()
            except Exception:
                log.debug("freshness flush failed", exc_info=True)
        # Heartbeat bridge_event — emit one row per minute so the
        # `runner_health.py` "NO bridge events in last 12h" check passes
        # even on quiet days where no trades fire. Without this, the
        # health check generates false alarms on weekends + low-volatility
        # weekdays. Throttled to ≤1/minute to keep the table small.
        try:
            self._maybe_record_heartbeat_event(
                now_iso, n_active=len(active))
        except Exception:
            log.debug("heartbeat bridge_event failed", exc_info=True)
        return records

    def _maybe_record_heartbeat_event(self, now_iso: str,
                                        *, n_active: int = 0) -> None:
        """Throttle: emit at most one runner_heartbeat row per minute.

        Called from BOTH the active path (after a tick processes
        deployments) AND the idle path (when no deployments are
        live/paper). Either way the heartbeat proves the loop is
        ticking — what matters for runner_health.py."""
        last = getattr(self, "_last_heartbeat_event_at", None)
        if last is not None:
            try:
                t0 = datetime.fromisoformat(last)
                t1 = datetime.fromisoformat(now_iso)
                if (t1 - t0).total_seconds() < 60:
                    return
            except Exception:
                pass
        from core import storage
        storage.record_bridge_event(
            self.db_path, now_iso,
            method="runner_heartbeat", ok=True, latency_ms=0,
            error=f"active_deps={n_active} alive_at_poll_s={self.poll_seconds}",
        )
        self._last_heartbeat_event_at = now_iso

    # ---- Internals ----
    def _run_loop(self) -> None:
        while self._running:
            t0 = time.time()
            try:
                self.tick_once()
            except Exception:
                log.exception("tick_once failed")
            elapsed = time.time() - t0
            # Bar-close-aware sleep. Default sleep is poll_seconds, but
            # if the next bar close (across all active deployments) is
            # closer than that, sleep until ~2s after that close so the
            # tick fires when a new bar is fresh on the broker. This
            # caps the bar-close-to-order-send latency at ~2s on the
            # smallest active TF, vs poll_seconds without it.
            #
            # Rationale: on M15 a 60s polling cadence puts the strategy
            # 0-60s late seeing a closed bar. The catalog's slippage
            # model (5% of ATR) assumes near-instant fill, so an extra
            # 30s avg of price drift is real PnL leakage vs backtest.
            # Bar-close-aware scheduling closes the gap to ~2s while
            # keeping cheap "no_new_bar" polls frequent enough that a
            # missed cycle never delays a real signal more than poll_s.
            sleep_for = max(0.5, self.poll_seconds - elapsed)
            try:
                next_close_in = self._seconds_until_next_bar_close()
                if next_close_in is not None and next_close_in > 0:
                    # Buffer: 2s after the close so the broker has the
                    # finalised bar in copy_rates. Any less and we may
                    # see the previous bar still as "last" briefly.
                    bar_aware = next_close_in + 2.0
                    if bar_aware < sleep_for:
                        sleep_for = bar_aware
            except Exception:
                # If TF parsing or timezone math fails, fall back to
                # poll_seconds — never block the loop.
                log.debug("bar-close-aware sleep failed, falling back",
                          exc_info=True)
            # Wake up early if stop() was called. We poll _running every
            # 0.5s so a Ctrl+C unblocks within half a second even if we
            # were planning to sleep longer for a bar close.
            for _ in range(int(sleep_for * 2)):
                if not self._running:
                    return
                time.sleep(0.5)

    # ---- Bridge consecutive-failure tracking (#6) ------------------------------
    def _record_bridge_failure(self, detail: str) -> None:
        """Increment consecutive-failure counter. At threshold, emit a
        CRITICAL log + a `bridge_alarm` row in bridge_events so the
        dashboard can show a red banner."""
        self._bridge_consecutive_failures += 1
        if (self._bridge_consecutive_failures
                >= self._bridge_alarm_threshold
                and not self._bridge_alarm_active):
            self._bridge_alarm_active = True
            log.critical(
                "BRIDGE ALARM — %d consecutive failures (latest: %s). "
                "Investigate MT5 bridge / EA / network. Runner will "
                "continue trying but no signals will fire until the "
                "bridge recovers.",
                self._bridge_consecutive_failures, detail,
            )
            try:
                from core import storage
                storage.record_bridge_event(
                    self.db_path,
                    datetime.now(timezone.utc).isoformat(),
                    method="bridge_alarm", ok=False, latency_ms=0,
                    error=(f"consecutive_failures="
                           f"{self._bridge_consecutive_failures} "
                           f"detail={detail[:200]}"),
                )
            except Exception:
                log.debug("could not record bridge_alarm event",
                          exc_info=True)

    def _record_bridge_success(self) -> None:
        """Reset consecutive-failure counter. If we were in alarm
        state, emit a `bridge_recovered` event."""
        was_in_alarm = self._bridge_alarm_active
        self._bridge_consecutive_failures = 0
        if was_in_alarm:
            self._bridge_alarm_active = False
            log.warning("BRIDGE ALARM cleared — recovered after "
                        "consecutive failures")
            try:
                from core import storage
                storage.record_bridge_event(
                    self.db_path,
                    datetime.now(timezone.utc).isoformat(),
                    method="bridge_recovered", ok=True, latency_ms=0,
                    error=None,
                )
            except Exception:
                log.debug("could not record bridge_recovered event",
                          exc_info=True)

    # ---- Bar-close-aware scheduling helper ---------------------------------
    _TF_SECONDS = {
        "M1":   60,
        "M5":   300,
        "M15":  900,
        "M30":  1800,
        "H1":   3600,
        "H4":   14400,
        "D1":   86400,
    }

    def _seconds_until_next_bar_close(self) -> Optional[float]:
        """How many seconds until the next bar close across all active
        deployments. Returns None when no usable TF info is available
        (caller falls back to poll_seconds).

        We pick the SMALLEST TF among active deployments — that bar
        closes most frequently, so the next close on it is the next
        "interesting moment" for any deployment. D1 deployments are
        evaluated on every M15 close too — the bar-edge dedupe in
        `_tick_deployment` makes that essentially free.
        """
        from core import deployment as _dep_mod
        try:
            deps = _dep_mod.load_deployments(self.login)
        except Exception:
            return None
        active = [d for d in deps if d.status in ("paper", "live")]
        if not active:
            return None
        tf_seconds = [
            self._TF_SECONDS.get(d.tf.upper())
            for d in active
            if d.tf.upper() in self._TF_SECONDS
        ]
        tf_seconds = [s for s in tf_seconds if s is not None]
        if not tf_seconds:
            return None
        smallest = min(tf_seconds)
        # Compute "next close" assuming UTC bars aligned to wall-clock
        # boundaries. M15 closes at :00, :15, :30, :45 of every hour.
        # H1 closes at :00. D1 closes at 00:00 UTC. This is the FTMO/
        # MT5 convention (broker-local server time may shift by tz, but
        # for active poll cadence the wall-clock alignment is right
        # to within seconds — close enough for scheduling).
        now = datetime.now(timezone.utc)
        secs_today = (now.hour * 3600 + now.minute * 60
                       + now.second + now.microsecond / 1e6)
        next_boundary = (int(secs_today / smallest) + 1) * smallest
        # If next_boundary > 86400, the bar closes at midnight tomorrow
        if next_boundary >= 86400:
            return 86400 - secs_today
        return next_boundary - secs_today

    def _tick_deployment(
        self,
        d,
        rec: TickRecord,
        *,
        block_opens_reason: Optional[str],
        open_positions: list,
    ) -> None:
        # Resolve strategy
        strat = self._get_strategy(d)
        if strat is None:
            rec.error = f"strategy `{d.strategy}` not registered"
            return

        # Edge-decay autolearner — auto-demote LIVE→PAPER if rolling
        # live R has been > 2σ below catalog R for ≥15 trades. The
        # assessment is bounded-cost (single SQL query, ~30 rows max
        # per deployment) so checking every tick is fine. Wrapped in
        # try/except so a decay-tracking failure never blocks trading.
        if d.status == "live" and self.enforce_edge_decay_demote:
            try:
                from core import edge_decay
                from core import edge_catalog as _ec
                edge_row = _ec.best_for(d.ticker, d.tf, d.strategy)
                if edge_row is not None:
                    catalog_R = float(
                        getattr(edge_row, "expectancy_per_R", 0.0)
                        or getattr(edge_row, "test_r", 0.0) or 0.0
                    )
                    a = edge_decay.assess(
                        self.db_path,
                        deployment_id=d.deployment_id,
                        catalog_R=catalog_R,
                    )
                    if a.should_demote:
                        now_iso = datetime.now(timezone.utc).isoformat(
                            timespec="minutes")
                        demote_note = (
                            f"[EDGE-DECAY DEMOTE {now_iso}] "
                            f"{a.reason}. Live mean R "
                            f"{a.rolling_mean_R:+.3f} vs catalog "
                            f"{a.catalog_R:+.3f} over "
                            f"{a.n_trades} trades. Re-check the "
                            f"catalog cell or wait for regime to "
                            f"recover before re-promoting."
                        )
                        try:
                            existing = dep_mod.load_deployments(self.login)
                            for dep_row in existing:
                                if dep_row.deployment_id == d.deployment_id:
                                    dep_row.status = "paper"
                                    dep_row.notes = (
                                        demote_note
                                        + (f"\n{dep_row.notes}"
                                           if dep_row.notes else "")
                                    )
                                    break
                            dep_mod.save_deployments(self.login, existing)
                        except Exception:
                            dep_mod.update_status(
                                self.login, d.deployment_id, "paper")
                        log.warning(
                            "EDGE-DECAY DEMOTE dep=%s z=%.2f "
                            "sustained_red=%d",
                            d.deployment_id,
                            a.z_score or 0.0,
                            a.sustained_red_trades,
                        )
                        rec.error = (
                            f"auto-demoted: edge-decay z="
                            f"{a.z_score:.2f}"
                            if a.z_score is not None else
                            "auto-demoted: edge-decay"
                        )
                        return
            except Exception:
                log.debug("edge_decay assess failed (non-fatal)",
                            exc_info=True)

        # Parity-fresh gate for LIVE — only checked when
        # enforce_parity_freshness=True (opt-in, default off).
        # Default: parity is a one-time DEPLOY-TIME gate. Once you've
        # promoted a strategy to live (via the parity-aware deploy
        # dialogs), the runner trusts that decision and doesn't re-gate
        # on every tick. User stays in control — explicit disable /
        # delete only.
        if d.status == "live" and self.enforce_parity_freshness:
            gate = parity_gate_mod.ParityGate(self.db_path)
            from dashboards.components.strategy_resolver import (
                resolve_base_strategy,
            )
            from dashboards.components.state import discover_strategies
            base = (resolve_base_strategy(d.strategy, discover_strategies())
                     or d.strategy)
            if not gate.is_recent(base):
                if self.auto_demote_on_parity_expiry:
                    # OPT-IN demotion (default OFF). Kept for users who
                    # explicitly want this older behaviour.
                    now_iso = datetime.now(timezone.utc).isoformat(
                        timespec="minutes")
                    demote_note = (
                        f"[AUTO-DEMOTED LIVE→PAPER {now_iso}] "
                        f"parity expired for base `{base}` (>24h since last "
                        f"pass). User opted into auto-demotion."
                    )
                    try:
                        existing = dep_mod.load_deployments(self.login)
                        for dep_row in existing:
                            if dep_row.deployment_id == d.deployment_id:
                                dep_row.status = "paper"
                                dep_row.notes = (
                                    demote_note
                                    + (f"\n{dep_row.notes}"
                                       if dep_row.notes else "")
                                )
                                break
                        dep_mod.save_deployments(self.login, existing)
                    except Exception:
                        dep_mod.update_status(
                            self.login, d.deployment_id, "paper")
                    log.warning("AUTO-DEMOTED dep=%s LIVE→PAPER",
                                  d.deployment_id)
                    rec.error = (f"parity expired for `{base}` — "
                                  f"auto-demoted (opt-in)")
                    return
                # ── Default path: stay LIVE, just block new opens ──
                # Existing positions on the broker continue normally
                # (their SL/TP are server-side). update_bar still runs
                # at the top of the tick to detect SL/TP fills.
                # We just refuse to OPEN any new position this tick.
                block_opens_reason = (
                    f"PARITY STALE for `{base}` — refresh via Strategy "
                    f"Library / Live page replay-parity button. "
                    f"Open positions ride server-side SL/TP."
                )
                log.warning(
                    "PARITY_STALE dep=%s base=`%s` — blocking new opens "
                    "but keeping status=live. Existing positions ride "
                    "to SL/TP. Run replay-parity to clear.",
                    d.deployment_id, base,
                )
                # Don't return — fall through so update_bar still runs
                # and existing positions can close on SL/TP/forced-flat.
                # The block_opens_reason routed to runner.tick will
                # refuse the new-position branch.

        # Fetch bars + keep on-disk parquet in sync.
        #
        # Pre-fix: runner pulled bars via copy_rates each tick and used
        # them in-memory. The on-disk parquet only updated when the
        # user manually ran `make refresh-data`, so the dashboard /
        # backtest / replay all saw stale data. Symptom: dashboard
        # said "last bar 6h ago" while the runner was happily ticking
        # on fresh bars — looked exactly like a broken runner.
        #
        # Fix: route every per-tick fetch through `data_sync.sync_to_now`
        # which (a) calls our existing candle_fetcher, (b) merges any
        # new bars into the parquet, (c) atomically writes (tmp+rename
        # so concurrent dashboard reads never see partial state).
        # Result: parquet is always within ~poll_seconds of the live
        # market, and the dashboard sees what the runner sees.
        #
        # Per-tick cache: multiple deployments on the same (ticker,
        # tf) — e.g. rsi_30_70 EURUSD M15 + ema_cross EURUSD M15 — share
        # one fetch + one parquet write. Cache key is (ticker, tf);
        # value is the merged DataFrame; cleared at the start of each
        # tick_once cycle.
        cache_key = (d.ticker, d.tf)
        cached = self._tick_data_cache.get(cache_key)
        if cached is not None:
            view = cached
        else:
            try:
                from core import data_sync
                view = data_sync.sync_to_now(
                    d.ticker, d.tf, self.candle_fetcher,
                    count=self.max_history_bars,
                )
            except Exception as e:
                rec.error = f"fetch bars: {type(e).__name__}: {e}"
                self._record_bridge_failure(f"sync_to_now: {e}")
                return
            if view is None or len(view) < 2:
                rec.error = "no bars returned"
                self._record_bridge_failure("no bars returned")
                return
            self._tick_data_cache[cache_key] = view
            # Successful fetch — reset the consecutive-failure counter.
            self._record_bridge_success()

        # Bar-edge dedupe — only act when a NEW bar has arrived
        last_bar_time = pd.Timestamp(view["time"].iloc[-1])
        last_seen = self._last_seen_bar.get(d.deployment_id)
        if last_seen is not None and last_bar_time <= last_seen:
            rec.n_skipped += 1
            rec.skip_reason = "no_new_bar"
            return
        self._last_seen_bar[d.deployment_id] = last_bar_time

        # Per-deployment hold check (Phase 30d): if this deployment
        # already has a live position on the broker, REFUSE to open
        # another. The strategy must wait for SL or TP to close the
        # current position first. Without this, a strategy that fires
        # a signal every M15 bar would stack 4 positions per hour
        # despite "1 trade per signal" being the intent.
        #
        # User feedback: "do not take another trade on same setup until
        # TP or SL is hit for same setup".
        if d.status == "live":
            already_holding = self._deployment_has_live_position(
                d, open_positions,
            )
            if already_holding:
                rec.n_skipped += 1
                rec.skip_reason = "already_holding"
                rec.error = (
                    f"already holding live position on {d.ticker} "
                    f"(ticket {already_holding}) — refusing new open. "
                    f"Wait for SL/TP."
                )
                log.debug(
                    "HOLD_GUARD dep=%s already holding ticket=%s — "
                    "skipping new open",
                    d.deployment_id, already_holding,
                )
                # Note: we DON'T rewind last_seen_bar here — we DO
                # consume this bar (the strategy already fired and we
                # chose to ignore). Otherwise we'd loop on every tick.
                return

        # ── Sizing inputs ────────────────────────────────────────────
        # Phase-27 dynamic sizing: risk_pct + account_balance + symbol_info
        # are passed to runner.tick which routes them to calc_lots().
        #
        # STRICT MODE for LIVE (Phase 30c): if any sizing input is missing,
        # we REFUSE to tick — silent under-sizing on real money is worse
        # than missing a trade. Paper tolerates the fallback.
        from core.symbol_info_loader import try_load as _try_load_sym
        from dashboards.components.state import (
            DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT,
        )

        sym_info = _try_load_sym(d.ticker)
        account_balance = self._get_account_balance()
        # Phase-32: adaptive risk auto-mode. When enabled in
        # risk_config.json, the per-deployment static risk_pct becomes
        # the MAXIMUM and the runner computes the actual per-trade
        # risk from FTMO buffer state every tick. When disabled,
        # falls back to static d.risk_pct (existing behavior).
        static_risk_pct = float(getattr(d, "risk_pct", 0.0) or 0.0)
        risk_pct = static_risk_pct
        try:
            from core import system_config as _sc
            sc_cfg = _sc.load_system_config(self.login)
            if sc_cfg.account_risk.use_adaptive_risk and account_balance > 0:
                from core import adaptive_risk as _ar
                from core import daily_pnl as _dpnl
                # Today's realized P&L since FTMO daily reset
                pnl_today = _dpnl.realized_pnl_today(
                    self.db_path,
                    rollover_hhmm=sc_cfg.account_risk.ftmo_daily_reset_utc,
                )
                ar_result = _ar.calculate(
                    current_equity=account_balance,
                    baseline_equity=sc_cfg.cost_model.starting_balance_usd,
                    daily_pnl_so_far=pnl_today,
                    daily_soft_cap_pct=sc_cfg.account_risk.daily_soft_cap_pct,
                    daily_hard_cap_pct=sc_cfg.account_risk.daily_loss_cap_pct,
                    total_loss_floor_pct=10.0,
                    target_trades_per_day=(
                        sc_cfg.account_risk.target_trades_per_day
                    ),
                    max_risk_pct=static_risk_pct or 1.0,
                    min_risk_pct=0.05,
                )
                # Phase-32 (b): record equity sample + apply recovery
                # factor (0.5 fragile / 1.5 stable peak / 1.0 normal).
                # Sample is recorded BEFORE classification so the
                # current tick is included in the regime read.
                try:
                    from core import equity_tracker as _et
                    _et.record_sample(self.db_path,
                                       equity=account_balance)
                    regime = _et.assess_now(
                        self.db_path,
                        baseline_equity=(
                            sc_cfg.cost_model.starting_balance_usd
                        ),
                    )
                    if regime.recovery_factor != 1.0:
                        ar_result = _ar.apply_recovery_factor(
                            ar_result,
                            recovery_factor=regime.recovery_factor,
                            max_risk_pct=static_risk_pct or 1.0,
                        )
                        log.info(
                            "equity_regime=%s factor=%.1fx (%s)",
                            regime.regime.value,
                            regime.recovery_factor, regime.reason,
                        )
                except Exception:
                    log.debug("equity_tracker failed (non-fatal)",
                              exc_info=True)
                if not ar_result.allow_trade:
                    rec.error = (
                        f"adaptive_risk REFUSED — {ar_result.reason}"
                    )
                    log.warning(
                        "adaptive_risk REFUSED dep=%s: %s",
                        d.deployment_id, ar_result.reason,
                    )
                    return
                risk_pct = ar_result.risk_pct
                log.info(
                    "adaptive_risk dep=%s static=%.2f%% → adaptive=%.2f%% "
                    "(%s)",
                    d.deployment_id, static_risk_pct, risk_pct,
                    ar_result.reason,
                )
        except Exception as e:
            log.warning(
                "adaptive_risk failed (%s) — falling back to static "
                "risk_pct=%.2f%% for dep=%s",
                e, static_risk_pct, d.deployment_id,
            )
            risk_pct = static_risk_pct
        mpu = DEFAULT_MONEY_PER_UNIT.get(d.ticker, 1.0)
        fallback_lots = float(DEFAULT_LOTS.get(d.ticker, 0.01))

        sizing_active = (sym_info is not None
                            and risk_pct > 0
                            and account_balance > 0)

        # LIVE deployments REFUSE to tick when sizing inputs are missing.
        # This is the single biggest silent-fail vector in the system —
        # a live order at fixed lots=0.01 ignores the user's risk %
        # entirely.
        if d.status == "live" and not sizing_active:
            missing = []
            if sym_info is None:
                missing.append(f"symbol_info[{d.ticker}]")
            if risk_pct <= 0:
                missing.append("risk_pct")
            if account_balance <= 0:
                missing.append("account_balance")
            rec.error = (
                f"LIVE_REFUSED: sizing inputs missing: {', '.join(missing)}. "
                f"Run `make refresh-symbol-info` to populate symbol_info, "
                f"or set risk_pct > 0 on this deployment."
            )
            log.error(
                "LIVE_REFUSED dep=%s missing=%s — refusing to tick "
                "(would silently use fallback lots=%g and ignore your "
                "risk_pct)",
                d.deployment_id, missing, fallback_lots,
            )
            return

        # PAPER deployments tolerate the fallback (educational mode).
        if sizing_active:
            log.debug(
                "sizing_inputs dep=%s risk_pct=%.2f%% equity=$%.0f "
                "tick_size=%g tick_value=%g vol_min=%g vol_step=%g",
                d.deployment_id, risk_pct, account_balance,
                sym_info.tick_size, sym_info.tick_value,
                sym_info.volume_min, sym_info.volume_step,
            )
        else:
            log.warning(
                "sizing_FALLBACK dep=%s [paper] risk_pct=%.2f%% "
                "equity=$%.0f sym_info=%s — using fixed lots=%g",
                d.deployment_id, risk_pct, account_balance,
                "missing" if sym_info is None else "ok",
                fallback_lots,
            )

        # News-blackout gate — refuse new opens within ±N min of any
        # red-folder event affecting this ticker. Layered into
        # block_opens_reason so existing positions ride server-side
        # SL/TP unaffected; only NEW opens get blocked.
        if (self.enforce_news_blackout
                and not block_opens_reason):
            try:
                from core import news_calendar
                signal_at = datetime.now(timezone.utc)
                blackout = news_calendar.find_blackout(
                    ticker=d.ticker,
                    at_utc=signal_at,
                    cache_path=self.db_path.parent / "news_calendar.json",
                    window_minutes=self.news_blackout_window_minutes,
                )
                if blackout is not None:
                    block_opens_reason = (
                        f"NEWS BLACKOUT: {blackout.reason}"
                    )
            except Exception:
                log.debug("news_calendar check failed (non-fatal)",
                            exc_info=True)

        # Correlation-cluster cap — refuse opens when the symbol's
        # cluster already has `correlation_cluster_cap` positions live.
        # Layered into block_opens_reason so existing positions ride
        # untouched; only NEW opens are blocked. Skipped when the
        # ticker is already in `open_positions` (we're going to close,
        # not open).
        if (self.enforce_correlation_cluster_cap
                and not block_opens_reason):
            try:
                from core import correlation_clusters
                this_dep_owns = any(
                    p.deployment_id == d.deployment_id
                    for p in open_positions
                )
                if not this_dep_owns:
                    cluster_check = correlation_clusters.check_cluster_open(
                        symbol=d.ticker,
                        open_positions=open_positions,
                        max_concurrent_per_cluster=(
                            self.correlation_cluster_cap),
                    )
                    if cluster_check.decision == "BLOCK":
                        block_opens_reason = (
                            f"CLUSTER CAP: {cluster_check.reason}"
                        )
            except Exception:
                log.debug("correlation_clusters check failed (non-fatal)",
                            exc_info=True)

        # Tick (paper engine + dynamic sizing OR fallback fixed lots)
        executor = self._get_paper_executor(d)
        result = runner_tick(
            view, executor, strat,
            symbol=d.ticker, tf=d.tf,
            money_per_unit_price=mpu,
            lots=fallback_lots,
            block_opens_reason=block_opens_reason,
            deployment_id=d.deployment_id,
            open_positions_snapshot=open_positions,
            position_guard_policy=self.position_guard_policy,
            # ── Phase-27 dynamic sizing — kicks in when ALL three set ──
            risk_pct=risk_pct if sizing_active else None,
            symbol_info=sym_info if sizing_active else None,
            account_balance=account_balance if sizing_active else None,
            max_lots=(getattr(d, "max_lots", None) or None) if sizing_active else None,
            max_money_risk_usd=(
                (getattr(d, "max_money_risk_usd", None) or None)
                if sizing_active else None
            ),
        )
        rec.n_opens = len(result.opens)
        rec.n_closes = len(result.closes)
        rec.n_skipped = (result.skipped_due_to_no_entry_window
                          + result.skipped_due_to_circuit_breaker
                          + result.skipped_due_to_position_guard)

        # ── Phase-32: write deployment freshness fields ──────────────────
        # Three timestamps tell three different stories:
        #   last_evaluated_at_utc        — runner ticked, alive proof
        #   last_signal_seen_at_utc      — strategy fired (open or not)
        #   last_open_succeeded_at_utc   — broker confirmed open (real $)
        #
        # Pre-fix all three stayed null and the user couldn't tell:
        #   "is the runner alive?" (last_evaluated)
        #   "did the strategy fire but get blocked?" (signal_seen)
        #   "did real money go into a position?" (open_succeeded)
        #
        # last_signal_at_utc is kept as an alias for last_signal_seen
        # for backwards compat with any old code that reads the old
        # field name.
        try:
            changed = False
            if result.last_evaluated_bar_utc:
                if d.last_evaluated_at_utc != result.last_evaluated_bar_utc:
                    d.last_evaluated_at_utc = result.last_evaluated_bar_utc
                    changed = True
            if result.last_signal_bar_utc:
                if d.last_signal_seen_at_utc != result.last_signal_bar_utc:
                    d.last_signal_seen_at_utc = result.last_signal_bar_utc
                    # Mirror to old field for backwards compat
                    d.last_signal_at_utc = result.last_signal_bar_utc
                    changed = True
            if changed:
                self._dep_dirty.add(d.deployment_id)
        except Exception:
            log.debug("freshness write failed", exc_info=True)

        # Persist closes (both paper + live use the same SQLite journal)
        for closed in result.closes:
            self._persist_closed_trade(d, closed)

        # If LIVE, mirror each open to the broker via LiveExecutor
        if d.status == "live" and result.opens:
            self._mirror_opens_to_broker(d, result.opens, rec)

        if result.errors:
            rec.error = "; ".join(result.errors[:3])

    def _mirror_opens_to_broker(self, d, opens, rec: TickRecord) -> None:
        if self.bridge_call is None:
            rec.error = "live: bridge_call not configured"
            return
        if self._live_executor is None:
            from core.config import load_config
            from core.time_guards import time_guard_cfg_from_risk_config
            cfg = load_config()
            self._live_executor = LiveExecutor(
                db_path=self.db_path,
                risk_config=cfg,
                time_guard_cfg=time_guard_cfg_from_risk_config(cfg),
                account_login=self.login,
                bridge_call=self.bridge_call,
            )
        # ── Phase-32 #7: ticket-already-open pre-flight guard ─────────
        # Before sending any new orders for this deployment, snapshot
        # current broker positions and refuse to send if a position on
        # the same symbol with the same magic/comment is already open.
        # Belt-and-braces with the existing _deployment_has_live_position
        # check upstream — if either fails open (e.g. positions_get
        # latency, race) we still won't double-up exposure.
        try:
            from core.mt5_account import MT5AccountClient
            client = MT5AccountClient(bridge_call=self.bridge_call)
            existing_positions = client.positions_get()
            existing_symbols = {
                getattr(p, "symbol", None) for p in (existing_positions or [])
            }
        except Exception as e:
            log.warning(
                "ticket_already_open guard: positions_get failed (%s) — "
                "proceeding without check (relying on _deployment_has_"
                "live_position upstream)", e,
            )
            existing_symbols = set()
        if d.ticker in existing_symbols:
            log.warning(
                "ticket_already_open guard BLOCK dep=%s — broker already "
                "has a position on %s; refusing new send",
                d.deployment_id, d.ticker,
            )
            rec.error = (
                f"refused new open — broker already holds position on "
                f"{d.ticker}"
            )
            return
        for op in opens:
            # ── Phase-32 #4: daily-risk accumulator pre-flight ─────────
            # Sum today's risk-at-open across ALL deployments on this
            # account; refuse new opens if this one would push the total
            # past the daily_loss_cap_pct × starting_balance threshold.
            # Pre-fix: each deployment's risk_pct cap was per-trade, so
            # 5 deployments could individually pass while collectively
            # blowing the FTMO daily-loss limit.
            try:
                from core import daily_risk_accumulator as _dra
                from core import system_config as _sysconf
                cfg = _sysconf.load_system_config(self.login)
                # risk-at-open in $ = lots × |entry - stop| × mpu
                # Same formula the position sizer uses.
                from dashboards.components.state import (
                    resolve_money_per_unit as _resolve_mpu,
                )
                mpu_for_calc = _resolve_mpu(d.ticker)
                price_diff = abs(op.entry_price - op.stop_price)
                this_risk_usd = float(op.lots) * price_diff * mpu_for_calc
                check = _dra.would_breach_cap(
                    login=self.login,
                    new_risk_usd=this_risk_usd,
                    daily_loss_cap_pct=cfg.account_risk.daily_loss_cap_pct,
                    starting_balance_usd=(
                        cfg.cost_model.starting_balance_usd
                    ),
                    rollover_hhmm=cfg.account_risk.ftmo_daily_reset_utc,
                )
                if check.would_breach:
                    log.warning(
                        "daily_risk_cap REFUSED dep=%s: %s",
                        d.deployment_id, check.reason,
                    )
                    rec.error = (rec.error + "; " if rec.error else "") + (
                        f"daily_risk_cap: {check.reason}"
                    )
                    continue
            except Exception as e:
                log.warning(
                    "daily_risk_accumulator pre-flight raised %s: %s — "
                    "proceeding without check (fail-open on the helper)",
                    type(e).__name__, e,
                )
                this_risk_usd = 0.0   # Will skip record_open below
            # ── Phase-32 #1: pre-flight tick check ───────────────────
            # Fetch live tick price; refuse the open if SL is already
            # breached or price has drifted more than 0.10 ATR adverse
            # since signal generation. Without this, a slow tick could
            # have us opening a position that's INSTANTLY in stop-out.
            try:
                from core import preflight_tick
                # FX major default; for indices the dashboard symbol_info
                # has tick_size which we should use, but the difference
                # only affects the "drift in pips" log message — the
                # absolute price comparison is correct regardless.
                point_size = 0.0001 if "USD" in d.ticker.upper() else 0.01
                pf = preflight_tick.check(
                    direction=op.direction,
                    signal_entry_price=op.entry_price,
                    signal_stop_price=op.stop_price,
                    atr_at_signal=getattr(op, "atr_at_signal_bar", 0.0) or 0.0,
                    bridge_call=self.bridge_call,
                    symbol=d.ticker,
                    point_size=point_size,
                )
            except Exception as e:
                log.warning(
                    "preflight_tick check raised %s: %s — proceeding "
                    "without it (fail-open on the helper to avoid "
                    "blocking trades on a code bug)",
                    type(e).__name__, e,
                )
                pf = None
            if pf is not None and not pf.should_open:
                log.warning(
                    "preflight_tick REFUSED dep=%s: %s",
                    d.deployment_id, pf.reason,
                )
                rec.error = (rec.error + "; " if rec.error else "") + (
                    f"preflight_tick refused: {pf.reason}"
                )
                # Don't retry — bar window has passed, signal is stale.
                # The next bar will produce a fresh signal if conditions hold.
                continue
            ikey = f"{d.deployment_id}:{op.opened_at_utc}"
            order_obj = None
            try:
                order_obj = self._live_executor.send_order(
                    symbol=d.ticker,
                    direction=op.direction,
                    lots=op.lots,
                    sl=op.stop_price, tp=op.target_price,
                    strategy=d.strategy,
                    idempotency_key=ikey,
                )
            except BridgeOrderRejected as e:
                rec.error = f"broker REJECTED: {e} (retcode={e.retcode})"
                # Force a re-tick on the next bar by rolling back the
                # last_seen_bar marker — the broker never accepted, so
                # the runner should retry when conditions change.
                self._last_seen_bar.pop(d.deployment_id, None)
                continue
            except Exception as e:
                rec.error = f"live send: {type(e).__name__}: {e}"
                self._last_seen_bar.pop(d.deployment_id, None)
                continue

            # ─── Post-open verification ───────────────────────────────
            # send_order returned ok=True with a ticket. But that's the
            # bridge's claim; the position MUST also appear in
            # positions_get. If it doesn't show up within 5s we treat the
            # send as a phantom — log CRITICAL, don't update last_seen
            # so the next tick can retry, and surface the error in
            # TickRecord.
            # Phase-32 #7: 15s verification window + per-tick already-open
            # check (in _mirror_opens_to_broker pre-flight). 5s wasn't long
            # enough for FTMO during NY-open peak — bridge can take 6-10s
            # for positions_get to reflect a new fill. Phantom-open false
            # positives caused the runner to retry on the next bar and
            # double-up exposure. 15s is well within reason for any
            # non-degenerate broker.
            verified = self._verify_position_on_broker(
                ticket=order_obj.ticket,
                symbol=d.ticker,
                max_wait_s=15.0,
            )
            if verified:
                log.info(
                    "✅ LIVE OPEN verified  dep=%s  ticket=%d "
                    "lots=%g  %s @ %g  SL=%g TP=%g",
                    d.deployment_id, order_obj.ticket, op.lots,
                    op.direction, op.entry_price,
                    op.stop_price, op.target_price,
                )
                # Real money is now committed → write the
                # last_open_succeeded_at_utc timestamp. Dashboard
                # shows this so the user can distinguish "signal
                # fired" from "position actually opened on broker".
                d.last_open_succeeded_at_utc = (
                    datetime.now(timezone.utc).isoformat()
                )
                self._dep_dirty.add(d.deployment_id)
                # Phase-32 #4: register risk-at-open with the daily
                # accumulator. Done AFTER broker confirmation so we
                # don't accumulate risk for failed sends. The
                # `this_risk_usd` was computed in the pre-flight
                # block above.
                if this_risk_usd > 0:
                    try:
                        from core import daily_risk_accumulator as _dra2
                        from core import system_config as _sc2
                        _cfg2 = _sc2.load_system_config(self.login)
                        _dra2.record_open(
                            login=self.login,
                            deployment_id=d.deployment_id,
                            risk_usd=this_risk_usd,
                            rollover_hhmm=(
                                _cfg2.account_risk.ftmo_daily_reset_utc
                            ),
                        )
                    except Exception:
                        log.exception("daily_risk record_open failed "
                                      "(non-fatal)")
                # Phase-32 #9: slippage tracker. Record signal vs
                # actual fill so we can validate the catalog's
                # 0.05×ATR slippage assumption against live behaviour.
                # Bridge response contains the fill price at "price"
                # or "price_open". Some bridge versions use "result.price".
                try:
                    from core import slippage_tracker as _slip
                    br = order_obj.bridge_response or {}
                    inner = br.get("data", br) if isinstance(br, dict) else {}
                    fill_price = (
                        inner.get("price")
                        or inner.get("price_open")
                        or br.get("price")
                        or 0.0
                    )
                    if fill_price:
                        _slip.record(
                            db_path=self.db_path,
                            deployment_id=d.deployment_id,
                            symbol=d.ticker, tf=d.tf,
                            direction=op.direction,
                            signal_entry_price=op.entry_price,
                            actual_fill_price=float(fill_price),
                            atr_at_signal=(
                                getattr(op, "atr_at_signal_bar", 0.0)
                                or 0.0
                            ),
                            signal_bar_utc=getattr(op, "opened_at_utc", None),
                        )
                except Exception:
                    log.debug("slippage_tracker record failed (non-fatal)",
                              exc_info=True)
                # Bug C: track ticket→context for close-reconciliation. We
                # capture the canonical entry/stop/target prices from the
                # signal here so we can later compute R-multiple from the
                # original stop level when the position closes server-side.
                try:
                    from core.live_close_reconciler import LiveOpenContext
                    ctx = LiveOpenContext(
                        ticket=int(order_obj.ticket),
                        deployment_id=d.deployment_id,
                        symbol=d.ticker,
                        tf=d.tf,
                        strategy=d.strategy,
                        direction=op.direction,
                        entry_price=float(op.entry_price),
                        stop_price=float(op.stop_price),
                        target_price=float(op.target_price),
                        lots=float(op.lots),
                        opened_at_utc=str(getattr(op, "opened_at_utc", None)
                                              or datetime.now(timezone.utc).isoformat()),
                    )
                    self._open_live_tickets[int(order_obj.ticket)] = ctx.to_dict()
                    self._save_state()
                except Exception:
                    log.exception(
                        "Bug C: failed to track ticket %d for close-reconciliation "
                        "— close will need backfill",
                        int(order_obj.ticket),
                    )
            else:
                msg = (
                    f"⚠ PHANTOM OPEN  dep={d.deployment_id} "
                    f"ticket={order_obj.ticket} send_order returned ok=True "
                    f"but position never appeared in positions_get within 5s"
                )
                log.critical(msg)
                rec.error = (rec.error + "; " if rec.error else "") + (
                    f"PHANTOM OPEN ticket={order_obj.ticket}"
                )
                self._last_seen_bar.pop(d.deployment_id, None)

    def _verify_position_on_broker(self, *, ticket: int, symbol: str,
                                       max_wait_s: float = 5.0) -> bool:
        """Poll positions_get for up to max_wait_s seconds, return True
        once a position with this ticket (OR matching symbol if ticket
        comparison fails) appears."""
        if self.bridge_call is None:
            return False
        from core.mt5_account import MT5AccountClient
        client = MT5AccountClient(bridge_call=self.bridge_call)
        deadline = time.time() + max_wait_s
        while time.time() < deadline:
            try:
                positions = client.positions_get()
            except Exception:
                time.sleep(0.5)
                continue
            for p in positions:
                if int(getattr(p, "ticket", 0)) == ticket:
                    return True
            time.sleep(0.5)
        return False

    def _ensure_run_row(self, d) -> None:
        """Idempotently insert a parent row in `runs` for this deployment.

        Required because `trades.run_id` has a FK on `runs.run_id` and
        `PRAGMA foreign_keys = ON` is set on every connection (see
        storage.connect). Without the parent row, every INSERT into
        trades fails with FOREIGN KEY constraint failed.

        Uses INSERT OR IGNORE on the unique run_id, so it's safe to
        call repeatedly across runner restarts. We cache the seeded
        deployment_ids in-memory so we don't issue the INSERT on every
        close.
        """
        run_id = d.deployment_id
        if run_id in self._runs_seeded:
            return
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            cfg = json.dumps({
                "deployment_id": run_id,
                "mode": d.status,
                "ticker": d.ticker,
                "tf": d.tf,
                "strategy": d.strategy,
                "synthetic": True,    # marks "this is a runner-seeded run"
            }, sort_keys=True)
            with storage.connect(self.db_path) as c:
                c.execute(
                    "INSERT OR IGNORE INTO runs "
                    "(run_id, started_at_utc, symbol, tf, strategy_name, "
                    " config_json, starting_balance) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, now_iso, d.ticker, d.tf, d.strategy,
                     cfg, 100_000.0),
                )
            self._runs_seeded.add(run_id)
        except Exception:
            log.exception("ensure_run_row failed for %s", run_id)
            raise

    def _next_trade_idx(self, run_id: str) -> int:
        """Return the next monotonic `trade_idx` for this deployment.

        On first use we hydrate from `MAX(trade_idx)+1 FROM trades` so
        we survive runner restarts without colliding on the PK
        (run_id, trade_idx). Subsequent calls increment in memory.
        """
        if run_id not in self._trade_idx_by_dep:
            try:
                with storage.connect(self.db_path) as c:
                    row = c.execute(
                        "SELECT COALESCE(MAX(trade_idx), -1) + 1 "
                        "FROM trades WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                self._trade_idx_by_dep[run_id] = int(row[0]) if row else 0
            except Exception:
                # If the read fails, default to 0; the INSERT path will
                # surface the real error.
                self._trade_idx_by_dep[run_id] = 0
        idx = self._trade_idx_by_dep[run_id]
        self._trade_idx_by_dep[run_id] = idx + 1
        return idx

    def _persist_closed_trade(self, d, closed) -> None:
        """Persist a closed trade row.

        SAFETY-CRITICAL. The circuit breaker reads `trades` for
        realised PnL, daily loss %, and consecutive-loss count. If
        this insert fails silently, the breaker becomes blind to real
        losses and won't HALT the runner even after the FTMO daily/
        total-loss limits are crossed.

        Pre-fix bugs (2026-05-08):
        1. trade_idx was hardcoded to 0 → every close after the first
           per-deployment violated PRIMARY KEY (run_id, trade_idx).
        2. trades.run_id has a FK to runs.run_id and PRAGMA
           foreign_keys=ON is on, but no parent runs row was ever
           created → even the FIRST INSERT failed.
        3. The exception was caught with log.exception(), so the
           runner kept ticking and trading despite a $8,742 drawdown
           that the breaker couldn't see.

        Post-fix:
        a. _ensure_run_row creates the FK parent idempotently.
        b. _next_trade_idx gives a monotonic counter, hydrated from
           DB on first use so restarts don't collide.
        c. idempotency_key derived from (deployment_id, trade_idx,
           exit_price, exit_time) prevents double-inserts on retry.
        d. On INSERT failure we record a `trade_persist_failed`
           bridge_alarm (so dashboards / health checks can see it),
           CRITICAL-log it, AND re-raise so the outer tick handler
           treats the deployment as failing this tick. This is
           strictly safer than the old swallow.
        """
        # 1. Make sure the FK parent row exists.
        self._ensure_run_row(d)

        # 2. Allocate the next trade_idx.
        run_id = d.deployment_id
        trade_idx = self._next_trade_idx(run_id)

        # 3. Build an idempotency key so retries don't double-insert.
        opened_iso = (str(getattr(closed, "opened_at_utc", None))
                       or datetime.now(timezone.utc).isoformat())
        closed_iso = (str(getattr(closed, "closed_at_utc", None))
                       or datetime.now(timezone.utc).isoformat())
        idem_key = (
            getattr(closed, "idempotency_key", None)
            or f"{run_id}:{trade_idx}:{closed_iso}"
        )

        # 4. Insert. On failure: roll back the in-memory counter, log
        #    CRITICAL, record a bridge_alarm, and re-raise. The outer
        #    tick wrapper will catch and continue, but the alarm makes
        #    the failure visible to the user instead of silent.
        try:
            with storage.connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO trades ("
                    " run_id, trade_idx, symbol, direction, "
                    " opened_at_utc, closed_at_utc, "
                    " entry_price, stop_price, target_price, exit_price, "
                    " lots, realized_pnl, r_multiple, close_reason, "
                    " mode, strategy, tf, idempotency_key"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "          ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, trade_idx,
                        d.ticker, closed.direction,
                        opened_iso, closed_iso,
                        closed.entry_price, closed.stop_price,
                        closed.target_price, closed.exit_price,
                        closed.lots, closed.realized_pnl,
                        getattr(closed, "r_multiple", 0.0),
                        getattr(closed, "close_reason", "?"),
                        d.status, d.strategy, d.tf,
                        idem_key,
                    ),
                )
        except Exception as e:
            # Roll back the counter so the next attempt re-uses this idx.
            self._trade_idx_by_dep[run_id] = trade_idx
            log.critical(
                "TRADE PERSIST FAILED dep=%s idx=%d err=%s — "
                "circuit breaker can no longer see realised PnL on "
                "this deployment. INVESTIGATE.",
                run_id, trade_idx, e,
            )
            try:
                storage.record_bridge_event(
                    self.db_path,
                    datetime.now(timezone.utc).isoformat(),
                    method="trade_persist_failed",
                    ok=False, latency_ms=0,
                    error=(f"{type(e).__name__}: {e} "
                           f"dep={run_id} idx={trade_idx}"),
                )
            except Exception:
                log.debug("could not record trade_persist_failed alarm",
                          exc_info=True)
            raise

        # Edge-decay autolearner: record this trade's R against the
        # catalog R for this cell. The autolearner aggregates rolling
        # samples and flips state to YELLOW/RED if live drifts > 2σ
        # below catalog. Wrapped in try/except so a decay-tracking
        # failure NEVER blocks the runner's main loop.
        try:
            from core import edge_decay
            from core import edge_catalog as _ec
            edge_row = _ec.best_for(d.ticker, d.tf, d.strategy)
            if edge_row is not None:
                catalog_R = float(getattr(edge_row, "expectancy_per_R", 0.0)
                                     or getattr(edge_row, "test_r", 0.0) or 0.0)
                live_R = float(getattr(closed, "r_multiple", 0.0) or 0.0)
                edge_decay.record_trade(
                    self.db_path,
                    deployment_id=d.deployment_id,
                    live_R=live_R,
                    catalog_R=catalog_R,
                )
        except Exception:
            log.debug("edge_decay record failed (non-fatal)",
                        exc_info=True)

    def _assert_account_match(self) -> bool:
        """Bug E: verify the MT5 bridge is currently serving the login
        this runner is configured for.

        Returns True if matched (or if there's no bridge configured —
        e.g. dry-run / paper-only setup). Returns False on mismatch
        and writes a `bridge_alarm` event with method='account_mismatch'.

        On mismatch:
          • Log CRITICAL (one-line, prominent in /tmp/mt5_runner.err)
          • Record bridge_alarm (throttled to once per minute)
          • Caller (tick_once) skips the entire tick — no bar fetch,
            no signal evaluation, no order_send, no reconciliation.

        This is fail-CLOSED. If the bridge errors out trying to read
        account_info, we treat that as a mismatch (better to halt
        than to send orders blindly).
        """
        if self.bridge_call is None:
            return True   # paper-only / dry-run runner — no bridge
        try:
            from core.mt5_account import MT5AccountClient
            client = MT5AccountClient(bridge_call=self.bridge_call)
            ai = client.account_info(force_refresh=True)
        except Exception as e:
            log.critical(
                "ACCOUNT MISMATCH CHECK: account_info() raised %s: %s — "
                "treating as mismatch and HALTING this tick", type(e).__name__, e,
            )
            self._record_account_mismatch_alarm(
                f"account_info raised {type(e).__name__}: {e}"
            )
            return False

        live_login = int(getattr(ai, "login", 0) or 0)
        if live_login == 0:
            log.critical(
                "ACCOUNT MISMATCH CHECK: bridge returned login=0 — "
                "MT5 may not be logged in. HALTING this tick."
            )
            self._record_account_mismatch_alarm("bridge returned login=0")
            return False

        if live_login != int(self.login):
            log.critical(
                "ACCOUNT MISMATCH: runner configured for login=%d but "
                "MT5 bridge is currently serving login=%d. REFUSING all "
                "bridge work this tick. Either log MT5 back into account "
                "%d, or add %d to account_manager and start a separate "
                "runner for it.",
                int(self.login), live_login, int(self.login), live_login,
            )
            self._record_account_mismatch_alarm(
                f"configured={int(self.login)} bridge_serving={live_login}"
            )
            return False

        return True

    def _record_account_mismatch_alarm(self, detail: str) -> None:
        """Throttle account_mismatch bridge_events to ≤1 per minute."""
        now_iso = datetime.now(timezone.utc).isoformat()
        last = self._last_account_mismatch_alarm_at
        if last is not None:
            try:
                t0 = datetime.fromisoformat(last)
                t1 = datetime.fromisoformat(now_iso)
                if (t1 - t0).total_seconds() < 60:
                    return
            except Exception:
                pass
        try:
            storage.record_bridge_event(
                self.db_path, now_iso,
                method="account_mismatch", ok=False, latency_ms=0,
                error=detail,
            )
            self._last_account_mismatch_alarm_at = now_iso
        except Exception:
            log.debug("could not record account_mismatch alarm",
                      exc_info=True)

    def _reconcile_live_closes(self, deployments) -> None:
        """Bug C: detect vanished live tickets and persist their closes.

        Compares `self._open_live_tickets` (state captured on each
        successful broker open) with the current set of bot-magic
        positions on the broker. Any ticket that's gone is reconciled
        by calling `core.live_close_reconciler.reconcile_vanished_tickets`
        which queries `history_deals_get`, builds a synthetic
        ClosedPosition, and writes the close to `trades` via
        `_persist_closed_trade`.

        Idempotent — `live_close_reconciler.already_persisted` skips
        any ticket whose close-row is already in `trades`.

        Side effects:
          • Inserts row into `trades` for each reconciled close
          • Inserts parent `runs` row if missing
          • Records `bridge_event method='close:{reason}'`
          • Updates edge_decay_samples (via _persist_closed_trade)
          • Removes the ticket from `_open_live_tickets` so we don't
            re-reconcile on the next tick
          • Calls `_save_state` to persist the trimmed open-tickets map
        """
        if self.bridge_call is None or not self._open_live_tickets:
            return

        # Get current bot-magic tickets on the broker
        try:
            from core.mt5_account import MT5AccountClient
            from core.live_close_reconciler import (
                BOT_MAGIC, LiveOpenContext, reconcile_vanished_tickets,
            )
            client = MT5AccountClient(bridge_call=self.bridge_call)
            current_positions = client.positions_get() or []
            current_bot_tickets = {
                int(getattr(p, "ticket", 0) or 0)
                for p in current_positions
                if int(getattr(p, "magic", 0) or 0) == BOT_MAGIC
            }
        except Exception:
            log.exception("reconcile_live_closes: positions_get failed")
            return

        # Quick exit if nothing has vanished
        prev_tickets = set(self._open_live_tickets.keys())
        if not (prev_tickets - current_bot_tickets):
            return

        # Hydrate ticket→ctx map from persisted dicts
        contexts: dict[int, LiveOpenContext] = {}
        for t, raw in list(self._open_live_tickets.items()):
            try:
                contexts[int(t)] = LiveOpenContext.from_dict(raw)
            except Exception:
                log.warning(
                    "reconcile_live_closes: dropping malformed ticket "
                    "context for %s — close cannot be reconciled "
                    "without it; backfill required", t,
                )
                self._open_live_tickets.pop(t, None)

        # Build deployment_id → Deployment lookup
        dep_by_id = {d.deployment_id: d for d in deployments}

        result = reconcile_vanished_tickets(
            open_contexts=contexts,
            current_bot_tickets=current_bot_tickets,
            bridge_call=self.bridge_call,
            db_path=self.db_path,
            persist_fn=self._persist_closed_trade,
            record_event_fn=storage.record_bridge_event,
            deployment_lookup_fn=dep_by_id.get,
        )

        # Trim reconciled + already-persisted from the open-tickets state
        for entry in result.get("reconciled", []):
            self._open_live_tickets.pop(int(entry["ticket"]), None)
        for ticket in result.get("skipped_already_persisted", []):
            self._open_live_tickets.pop(int(ticket), None)
        # `unmatched` stays in state — broker history may not reflect
        # the close yet (bridge lag); we'll retry next tick. After
        # 48h with no match the runtime gives up; the backfill script
        # is the recovery path for those.

        if (result.get("reconciled") or result.get("errors")
                or result.get("skipped_already_persisted")):
            self._save_state()

    def _open_positions_snapshot(self, deployments) -> list:
        """Build a list of OpenPosition for the position guard. Combines
        in-memory paper executors with live broker positions."""
        out = []
        # Paper positions
        for d in deployments:
            if d.status != "paper":
                continue
            ex = self._executors_paper.get(d.deployment_id)
            if ex is None:
                continue
            try:
                if ex.has_position(d.ticker):
                    pos = ex.get_position(d.ticker)
                    out.append(position_guard.OpenPosition(
                        deployment_id=d.deployment_id,
                        symbol=d.ticker, side=pos.direction,
                        lots=pos.lots,
                        opened_at_utc=str(getattr(pos, "opened_at_utc", "")),
                    ))
            except Exception:
                continue
        # Live positions (best-effort — bridge may be offline)
        if self.bridge_call is not None:
            try:
                from core.mt5_account import MT5AccountClient
                client = MT5AccountClient(bridge_call=self.bridge_call)
                # The bot always opens with magic=999_001 (see
                # core/live_executor.py). Positions with magic=0 are
                # PHANTOMS — opened manually in MT5 or by an old runner.
                # They MUST NOT block the bot's guards (position_guard +
                # correlation_clusters), otherwise a single XAUUSD manual
                # trade can lock out the entire metals_long cluster cap.
                # Bot-magic positions count; phantoms get filtered.
                BOT_MAGIC = 999_001
                phantom_count = 0
                for bp in client.positions_get():
                    magic = int(getattr(bp, "magic", 0) or 0)
                    sym = getattr(bp, "symbol", "")
                    if magic != BOT_MAGIC:
                        phantom_count += 1
                        log.debug(
                            "skipping phantom position in guard snapshot: "
                            "ticket=%s symbol=%s magic=%s lots=%s",
                            getattr(bp, "ticket", "?"), sym, magic,
                            getattr(bp, "volume", "?"),
                        )
                        continue
                    side = "LONG" if int(getattr(bp, "type", 0)) == 0 else "SHORT"
                    matched = next(
                        (d for d in deployments
                         if d.ticker == sym and d.status == "live"),
                        None,
                    )
                    out.append(position_guard.OpenPosition(
                        deployment_id=(matched.deployment_id
                                          if matched else f"unknown:{sym}"),
                        symbol=sym, side=side,
                        lots=float(getattr(bp, "volume", 0.0)),
                        opened_at_utc=str(getattr(bp, "time_setup", "")),
                    ))
                if phantom_count > 0:
                    log.info("guard snapshot: skipped %d phantom "
                              "position(s) (magic != %d)",
                              phantom_count, BOT_MAGIC)
            except Exception:
                pass
        return out

    def _count_open_positions(self) -> int:
        if self.bridge_call is None:
            return 0
        try:
            from core.mt5_account import MT5AccountClient
            return len(MT5AccountClient(bridge_call=self.bridge_call)
                          .positions_get())
        except Exception:
            return 0

    def _force_flat_if_close_window(
        self, now_iso: str, deployments: list,
    ) -> None:
        """Enforce weekend / daily forced-flat for LIVE positions.

        Mirrors paper_executor.update_bar's forced-flat logic for the
        live broker. Called once per tick. Idempotent: if the close
        window has already fired and the position is gone, this is a
        no-op. Skips gracefully if no bridge or no time-guard cfg.

        Pre-fix the live runner had no such logic — backtest gates +
        paper executor gates fired weekend-flat at Friday 19:55 UTC,
        but the live runner just kept ticking. A Friday-afternoon
        position would sit through the weekend, exposing the FTMO
        challenge to weekend gap risk on Sunday 22:00 UTC market open.
        """
        if self.bridge_call is None:
            return
        # Build the time-guard cfg from the global RiskConfig + apply
        # any per-deployment overrides. We only need the WEEKEND_FLAT
        # flag — daily-flat is more nuanced (asset-class dependent)
        # and we leave that to the broker's session close for now.
        from core.config import load_config
        from core.time_guards import (
            time_guard_cfg_from_risk_config, needs_weekend_flat,
            needs_daily_flat,
        )
        from datetime import datetime
        try:
            cfg = load_config()
        except Exception:
            return
        tg_cfg = time_guard_cfg_from_risk_config(cfg)
        # Parse the now timestamp
        try:
            now_dt = datetime.fromisoformat(now_iso)
        except Exception:
            return
        weekend_fire = (
            tg_cfg.weekend_flat_all
            and needs_weekend_flat(now_dt, tg_cfg)
        )
        if not weekend_fire and not tg_cfg.enforce_daily_flat:
            # Skip the daily-flat check too — neither fires.
            return
        # Iterate broker positions; for each, check whether the
        # appropriate flag is true and close if so.
        try:
            from core.mt5_account import MT5AccountClient
            client = MT5AccountClient(bridge_call=self.bridge_call)
            broker_positions = client.positions_get()
        except Exception:
            log.warning("force-flat: couldn't fetch broker positions")
            return
        for bp in broker_positions:
            sym = getattr(bp, "symbol", "")
            ticket = getattr(bp, "ticket", None)
            if not sym or ticket is None:
                continue
            close_reason = None
            if weekend_fire:
                close_reason = "weekend_flat"
            elif (tg_cfg.enforce_daily_flat
                  and needs_daily_flat(sym, now_dt, tg_cfg)):
                close_reason = "daily_close_flat"
            if close_reason is None:
                continue
            # Find the matching deployment to log under
            matched = next(
                (d for d in deployments
                 if d.ticker == sym and d.status in ("live", "paper")),
                None,
            )
            try:
                resp = self.bridge_call({
                    "method": "close_position",
                    "ticket": int(ticket),
                })
                ok = bool(resp.get("ok", False)) if isinstance(
                    resp, dict) else False
                log.info(
                    "force-flat %s: ticket=%s sym=%s reason=%s ok=%s",
                    matched.deployment_id if matched else "(unmatched)",
                    ticket, sym, close_reason, ok,
                )
                # Record event so health checks see real activity
                from core import storage
                storage.record_bridge_event(
                    self.db_path, now_iso,
                    method=f"close:{close_reason}",
                    ok=ok, latency_ms=0,
                    error=(None if ok
                           else f"ticket={ticket} sym={sym} resp={resp}"),
                )
            except Exception as e:
                log.exception("force-flat close failed: %s", e)

    def _deployment_has_live_position(self, d, open_positions) -> int:
        """Return the broker ticket if THIS deployment is currently
        holding a live position on the broker, else 0.

        Match rule: same ticker AND comment contains the strategy name
        (we stamp the comment with strategy in LiveExecutor.send_order).
        Falls back to "any same-symbol position by this account" when
        the comment doesn't match — pessimistic: when in doubt, refuse
        the duplicate open.
        """
        if not open_positions:
            return 0
        strat_lower = (d.strategy or "").lower()
        # Same-symbol candidates
        candidates = [p for p in open_positions
                       if getattr(p, "symbol", "") == d.ticker]
        if not candidates:
            return 0
        # Best match: comment contains strategy name (or its base prefix)
        base = strat_lower.rsplit("_", 2)[0] if "_" in strat_lower else strat_lower
        for p in candidates:
            cmt = (getattr(p, "comment", "") or "").lower()
            if (strat_lower and strat_lower in cmt) or (base and base in cmt):
                return int(getattr(p, "ticket", 0))
        # Fallback: same symbol, no other deployment owns it
        # (pessimistic — refuse to double-up)
        return int(getattr(candidates[0], "ticket", 0))

    def _get_account_balance(self) -> float:
        """Live broker equity — preferred source for risk-pct sizing.

        Falls back to FTMO baseline equity from account_manager when the
        bridge is offline or returns garbage. NEVER returns 0 (caller's
        sizing math defaults to off when balance is 0).
        """
        if self.bridge_call is not None:
            try:
                from core.mt5_account import MT5AccountClient
                info = MT5AccountClient(bridge_call=self.bridge_call) \
                    .account_info(force_refresh=True)
                if info.equity > 0:
                    return float(info.equity)
            except Exception:
                pass
        # Fallback — baseline from account_manager
        try:
            acct = account_manager.get_account(self.login)
            if acct is not None and acct.effective_baseline_equity > 0:
                return float(acct.effective_baseline_equity)
        except Exception:
            pass
        return 100_000.0  # last-resort default

    def _get_strategy(self, d):
        key = d.deployment_id        # cache per-deployment, not per-base-name
        if key in self._strategies_cache:
            return self._strategies_cache[key]
        from dashboards.components.state import discover_strategies
        from dashboards.components.strategy_resolver import resolve_base_strategy
        registry = discover_strategies()
        base = resolve_base_strategy(d.strategy, registry) or d.strategy
        if base not in registry:
            return None
        StratCls, ParamsCls = registry[base]
        # Pull params from THREE possible sources, in priority order:
        #   1. catalog source_config_json (rebaseline run that produced
        #      the deploy_safe verdict — exact reproduction)
        #   2. deployment.params (whatever the user/Composer stashed)
        #   3. ParamsCls() defaults (last resort)
        # Pre-fix: only #3 was used. That meant a `rsi_25_75` variant
        # would silently run with oversold=30/overbought=70 (defaults),
        # giving live behavior that DOES NOT match the backtest the
        # user clicked Deploy on. Same for any custom variant whose
        # name decoded to a base but whose params didn't.
        cfg: dict = {}
        try:
            from core import edge_catalog as _ec
            edge_row = _ec.best_for(d.ticker, d.tf, d.strategy)
            if edge_row is not None:
                src_json = getattr(edge_row, "source_config_json", "") or ""
                if src_json:
                    import json as _json
                    cfg = _json.loads(src_json) if isinstance(src_json, str) else {}
        except Exception:
            cfg = {}
        # Layer the deployment.params on top — they win over catalog
        # cfg if both define the same key (user override > catalog).
        try:
            for k, v in (d.params or {}).items():
                cfg[k] = v
        except Exception:
            pass
        # Always honour the deployment's `long_only` checkbox — it is
        # an OUTSIDE-of-params field on Deployment, but the strategy
        # constructor takes it as a kwarg.
        if hasattr(d, "long_only") and d.long_only is not None:
            cfg["long_only"] = bool(d.long_only)
        try:
            if ParamsCls is None:
                inst = StratCls()
            else:
                import dataclasses as _dc
                fields = {f.name for f in _dc.fields(ParamsCls)}
                kwargs = {k: v for k, v in cfg.items() if k in fields}
                inst = StratCls(ParamsCls(**kwargs)) if kwargs else \
                       StratCls(ParamsCls())
        except Exception:
            return None
        self._strategies_cache[key] = inst
        return inst

    def _get_paper_executor(self, d) -> PaperExecutor:
        ex = self._executors_paper.get(d.deployment_id)
        if ex is None:
            from core.config import load_config
            from core.time_guards import time_guard_cfg_from_risk_config
            cfg = load_config()
            ex = PaperExecutor(
                max_open_positions=1,
                commission_per_trade=3.0,
                slippage_per_fill_atr_frac=0.1,
                time_guard_cfg=time_guard_cfg_from_risk_config(cfg),
                mode="paper" if d.status == "paper" else "live",
            )
            self._executors_paper[d.deployment_id] = ex
        return ex

    def _record_events(self, records: list) -> None:
        for r in records:
            self._recent_events.append(r)
        if len(self._recent_events) > self.max_event_log:
            self._recent_events = self._recent_events[-self.max_event_log:]

    def _maybe_capture_forensic_snapshot(self, now_iso: str,
                                            deployments: list) -> None:
        """Capture system state once every 60s for post-mortem analysis.

        Bounded-cost: one INSERT, ~1ms. Wrapped in try/except so a
        snapshot failure NEVER blocks the runner. The forensic table
        accumulates ~525k rows/year on this cadence — pruned annually
        by the prune_older_than helper.
        """
        try:
            from datetime import datetime as _dt, timezone as _tz
            # Throttle to 60s — even if tick_once fires every 5s the
            # forensic write is rate-limited.
            if self._last_forensic_capture_utc is not None:
                last = _dt.fromisoformat(self._last_forensic_capture_utc)
                now = _dt.fromisoformat(now_iso)
                if (now - last).total_seconds() < 60:
                    return

            from core import forensic_snapshot
            from core import account_manager

            # Equity / balance — from MT5 account_info if available
            equity_usd = None
            balance_usd = None
            try:
                from core.mt5_account import MT5AccountClient
                info = MT5AccountClient().account_info()
                equity_usd = float(getattr(info, "equity", 0) or 0)
                balance_usd = float(getattr(info, "balance", 0) or 0)
            except Exception:
                pass

            # Open positions count
            n_open = 0
            try:
                from core.mt5_account import MT5AccountClient
                n_open = len(MT5AccountClient().positions_get())
            except Exception:
                pass

            # Daily realized P&L
            daily_real = 0.0
            try:
                from core import daily_pnl as _dpnl
                from core.config import load_config
                cfg = load_config(self.login)
                rollover = getattr(cfg, "ftmo_rollover_hhmm", "00:00")
                daily_real = float(_dpnl.realized_pnl_today(
                    self.db_path, rollover_hhmm=rollover) or 0.0)
            except Exception:
                pass

            # Equity regime + ftmo buffer
            regime = None
            buffer_pct = None
            try:
                from core import equity_tracker as _et
                from core.account_manager import get_account
                acct = get_account(self.login)
                if acct is not None:
                    baseline = float(acct.effective_baseline_equity)
                    if equity_usd is not None and baseline > 0:
                        # FTMO total-loss floor at 10% below baseline
                        total_floor = baseline * 0.90
                        buffer_pct = (equity_usd - total_floor) / baseline * 100
                    assessment = _et.assess_now(self.db_path,
                                                    baseline_equity=baseline)
                    if assessment is not None:
                        regime = getattr(assessment, "regime", None)
                        if regime is not None and hasattr(regime, "value"):
                            regime = regime.value
            except Exception:
                pass

            # Active deployments
            active_count = sum(
                1 for d in deployments
                if d.status in ("paper", "live")
            )
            estop = bool(account_manager.emergency_stop_active())

            forensic_snapshot.capture(
                self.db_path,
                login=self.login,
                equity_usd=equity_usd, balance_usd=balance_usd,
                n_open_positions=n_open,
                daily_realized_pnl_usd=daily_real,
                ftmo_buffer_remaining_pct=buffer_pct,
                equity_regime=regime,
                active_deployments_count=active_count,
                emergency_stop_active=estop,
                captured_at_utc=now_iso,
            )
            self._last_forensic_capture_utc = now_iso
        except Exception:
            log.debug("forensic snapshot failed (non-fatal)",
                        exc_info=True)

    @property
    def recent_events(self):
        return list(self._recent_events)

    @property
    def heartbeat_at_utc(self):
        return self._heartbeat_at_utc
