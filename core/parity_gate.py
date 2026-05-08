"""
parity_gate.py — wraps storage.parity_log to expose:
  - record_pass(strategy, divergence_dollars, at_utc=None)
  - last_pass_for(strategy_name) -> Optional[(datetime, divergence)]
  - is_recent(strategy_name, max_age_hours=24) -> bool

The replay-parity test (tests/test_replay_parity.py) calls record_pass on
green so live_executor's pre-flight check #5 can confirm parity is fresh.

Bug A fix (2026-05-08) — DB-split tolerance + strategy-alias resolver:

  Pre-fix the gate stored parity passes wherever record_pass was called
  (the dashboards write to REPO/data/v2.db; the runner instantiates
  LiveExecutor with the per-account db at data/accounts/<login>/v2.db),
  so writers and readers were looking at different files. Worse, the
  parity_log key was the literal `strategy` string used by the writer
  ("rsi_meanrev"), but the runner passed the deployment's `strategy`
  field which is the variant name ("rsi_30_70" / "rsi_25_75" / ...).
  The result: every live signal was preflight-denied because the gate
  could find no recent parity pass under that exact key, and the bot
  never sent a real order to the broker.

  Post-fix:
  1. ParityGate.is_recent / last_pass_for try a list of strategy
     candidates: the original name first, then resolved via the
     dashboards.components.strategy_resolver (so "rsi_30_70" matches a
     parity row written under "rsi_meanrev").
  2. ParityGate accepts a list of fallback DB paths. is_recent /
     last_pass_for check the primary DB first, then walk the fallbacks
     in order (typically the main REPO/data/v2.db).
  3. record_pass writes to ALL configured DBs (primary + fallbacks)
     when mirror=True (default), so future runs see the pass on either
     side of the split. Set mirror=False to opt out.

  These changes are backward-compatible: existing call sites that
  instantiate ParityGate(db_path) keep working, just with the alias
  resolver enabled. The fallback path is opt-in via the new
  fallback_db_paths kwarg.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from core import storage

log = logging.getLogger(__name__)


def _strategy_aliases(strategy: str) -> list[str]:
    """Return all strategy keys that should be checked for this
    deployment-level strategy name.

    Order matters: callers try them in sequence and return on first
    hit. We always include the literal `strategy` first, then the
    base-name resolution if available (e.g., "rsi_30_70" → "rsi_meanrev",
    "donchian_20" → "donchian_breakout", "ema_cross_12_26" → "ema_cross").
    """
    out: list[str] = [strategy]
    try:
        from dashboards.components.strategy_resolver import (
            resolve_base_strategy,
        )
        # discover_strategies lives in dashboards.components.state (and
        # also dashboards.control as a shim) — NOT in `strategies`.
        try:
            from dashboards.components.state import (
                discover_strategies,
            )
        except ImportError:
            from dashboards.control import discover_strategies   # noqa
        registry = discover_strategies()
        base = resolve_base_strategy(strategy, registry)
        if base and base != strategy and base not in out:
            out.append(base)
    except Exception:
        # Resolver may not be importable in non-dashboard contexts
        # (CI, smoke tests). Fall back to literal-only matching, plus
        # the static alias map below so common variants still resolve
        # without a registry.
        log.debug("strategy_resolver unavailable — using static alias map",
                  exc_info=True)
        static_base = _STATIC_BASE_ALIAS.get(strategy.split("_")[0])
        if static_base and static_base not in out:
            out.append(static_base)
        # Also try progressive suffix-trim (e.g. ema_cross_9_20 → ema_cross)
        parts = strategy.split("_")
        for cut in range(2, 0, -1):
            cand = "_".join(parts[:-cut])
            if cand and cand not in out and cand != strategy:
                out.append(cand)
    return out


# Static alias map — used as a fallback when the dashboards registry
# isn't importable (CI, smoke tests). Mirrors the canonical map in
# dashboards/components/strategy_resolver.py — keep them in sync.
_STATIC_BASE_ALIAS: dict[str, str] = {
    "donchian":  "donchian_breakout",
    "rsi":       "rsi_meanrev",
    "bbands":    "bbands_meanrev",
    "first30":   "first30_meanrev",
}


class ParityGate:
    def __init__(
        self,
        db_path: str | Path,
        *,
        fallback_db_paths: Optional[Iterable[str | Path]] = None,
    ):
        self.db_path = Path(db_path)
        # Filter to existing distinct paths so we don't probe missing
        # files / the same DB twice.
        seen = {self.db_path.resolve()}
        self.fallback_db_paths: list[Path] = []
        for p in (fallback_db_paths or []):
            pp = Path(p)
            try:
                rp = pp.resolve()
            except OSError:
                rp = pp
            if rp in seen:
                continue
            seen.add(rp)
            self.fallback_db_paths.append(pp)
        storage.init_schema(self.db_path)
        # Initialise fallbacks too so a fresh main DB doesn't crash
        # the fallback read path. Cheap on already-initialised DBs.
        for fb in self.fallback_db_paths:
            try:
                storage.init_schema(fb)
            except Exception:
                log.debug("init_schema failed for fallback %s",
                          fb, exc_info=True)

    # ── Public API ──────────────────────────────────────────────────
    def record_pass(
        self,
        strategy: str,
        divergence_dollars: float,
        at_utc: datetime | None = None,
        *,
        mirror: bool = True,
    ) -> None:
        """Record a parity pass.

        With mirror=True (default), writes to the primary DB AND every
        configured fallback. This eliminates the writer/reader split
        bug — both sides of the historical split see the new pass.
        """
        ts = (at_utc or datetime.now(timezone.utc)).isoformat()
        storage.record_parity_pass(
            self.db_path, strategy, ts, divergence_dollars,
        )
        if not mirror:
            return
        for fb in self.fallback_db_paths:
            try:
                storage.record_parity_pass(
                    fb, strategy, ts, divergence_dollars,
                )
            except Exception:
                log.debug("mirror parity pass to %s failed",
                          fb, exc_info=True)

    def last_pass_for(
        self, strategy: str,
    ) -> tuple[datetime, float] | None:
        """Return the most recent (timestamp, divergence) across all
        configured DBs and across all strategy aliases. Returns None
        if no pass is found anywhere.
        """
        candidates = _strategy_aliases(strategy)
        best: tuple[datetime, float] | None = None
        for db in [self.db_path, *self.fallback_db_paths]:
            for s in candidates:
                row = self._safe_last_pass(db, s)
                if row is None:
                    continue
                ts_str, div = row
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if best is None or ts > best[0]:
                    best = (ts, div)
        return best

    def is_recent(
        self,
        strategy: str,
        *,
        max_age_hours: float = 24.0,
        now_utc: datetime | None = None,
    ) -> bool:
        last = self.last_pass_for(strategy)
        if last is None:
            return False
        ts, _ = last
        now = now_utc or datetime.now(timezone.utc)
        return (now - ts) <= timedelta(hours=max_age_hours)

    # ── Internals ───────────────────────────────────────────────────
    @staticmethod
    def _safe_last_pass(db_path: Path, strategy: str):
        try:
            return storage.last_parity_pass(db_path, strategy)
        except Exception:
            log.debug("last_parity_pass(%s, %s) failed",
                      db_path, strategy, exc_info=True)
            return None
