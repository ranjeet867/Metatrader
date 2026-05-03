"""
live_executor.py — INVARIANT-6: NO LIVE ORDERS WITHOUT GATES.

send_order() runs SEVEN pre-flight checks; any one failing returns a
SafetyCheckRejection without contacting the bridge. Every refusal is logged
to data/v2.db's bridge_events (with method='preflight_deny').

The seven gates (in order — short-circuits on first failure):
  1. data/EMERGENCY_STOP file does NOT exist (re-checked every send)
  2. account login is in cfg.live_safety.allowed_accounts (or [] = open)
  3. risk_engine.account_daily_loss_pct < cfg.daily_loss_cap_pct
  4. risk_engine.consecutive_losses for (symbol, strategy) within cooldown
  5. parity_gate says strategy's last replay-parity within max_age_hours,
     OR cfg.live_safety.override_parity_recency is True
  6. idempotency_key not seen in last seen_window_seconds (default 3600)
     — prevents bridge-timeout retries duplicating fills
  7. NOT in_no_entry_window(now_utc, time_guard_cfg) — refuses opens
     in the last 30 minutes before US session close

The actual order send goes through a dependency-injected bridge_call so
tests can mock it. No real orders ever sent in test.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from core import storage
from core.config import RiskConfig
from core.parity_gate import ParityGate
from core.risk_engine import LiveRiskTracker
from core.time_guards import TimeGuardCfg, in_no_entry_window


EMERGENCY_STOP_FILENAME = "EMERGENCY_STOP"


# ---------------------------------------------------------------------------
# Errors + result types
# ---------------------------------------------------------------------------

class SafetyCheckRejection(Exception):
    """A pre-flight check refused this order. .reason is the human string."""
    def __init__(self, check_id: str, reason: str):
        super().__init__(f"[{check_id}] {reason}")
        self.check_id = check_id
        self.reason = reason


@dataclass
class LiveOrder:
    """Metadata for a sent (mocked or real) order."""
    ticket: int
    symbol: str
    direction: str
    lots: float
    sl: float
    tp: float
    sent_at_utc: str
    idempotency_key: str
    bridge_response: dict = field(default_factory=dict)


@dataclass
class CloseResult:
    ticket: int
    closed_at_utc: str
    reason: str
    bridge_response: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LiveExecutor
# ---------------------------------------------------------------------------

BridgeCallFn = Callable[[str, dict], dict]


class LiveExecutor:
    """Sends real orders via the MT5 file bridge — gated by 7 pre-flight checks.

    Args:
        db_path: data/v2.db. Used for risk_engine + parity_gate + bridge_events.
        risk_config: full RiskConfig from data/risk_config.json.
        time_guard_cfg: from time_guard_cfg_from_risk_config(risk_config).
        account_login: the MT5 account login this executor is bound to (used
            for check #2 and recorded on every order). Required.
        bridge_call: dependency-injected bridge — defaults to mt5_account's
            file-bridge transport. Tests pass a mock.
        emergency_stop_dir: path containing the EMERGENCY_STOP sentinel file.
            Default: REPO/data/.
        seen_window_seconds: idempotency-key dedup window (default 1h).
        risk_tracker: LiveRiskTracker; auto-built from risk_config if None.
        parity_gate: ParityGate; auto-built if None.
    """

    def __init__(self, *,
                 db_path: str | Path,
                 risk_config: RiskConfig,
                 time_guard_cfg: TimeGuardCfg,
                 account_login: int,
                 bridge_call: Optional[BridgeCallFn] = None,
                 emergency_stop_dir: Optional[Path] = None,
                 seen_window_seconds: float = 3600.0,
                 risk_tracker: Optional[LiveRiskTracker] = None,
                 parity_gate: Optional[ParityGate] = None,
                 parity_max_age_hours: float = 24.0):
        self.db_path = Path(db_path)
        self.risk_config = risk_config
        self.time_guard_cfg = time_guard_cfg
        self.account_login = int(account_login)
        self._call = bridge_call
        self.emergency_stop_dir = Path(emergency_stop_dir) if emergency_stop_dir \
                                    else self.db_path.parent
        self.seen_window_seconds = float(seen_window_seconds)
        self.parity_max_age_hours = float(parity_max_age_hours)

        self.risk_tracker = risk_tracker or LiveRiskTracker(
            db_path=self.db_path,
            max_consecutive_losses=risk_config.max_consecutive_losses,
            cooldown_minutes=240,
            daily_loss_cap_pct=risk_config.daily_loss_cap_pct,
        )
        self.parity_gate = parity_gate or ParityGate(self.db_path)

        # In-memory dedup of recent idempotency_keys: key → seen_at_unix
        self._seen_keys: dict[str, float] = {}

    # --- pre-flight checks (return reason string on FAIL, None on PASS) ---

    def _check_emergency_stop(self) -> Optional[str]:
        path = self.emergency_stop_dir / EMERGENCY_STOP_FILENAME
        if path.exists():
            return f"EMERGENCY_STOP file present: {path}"
        return None

    def _check_account_allowed(self) -> Optional[str]:
        allowed = self.risk_config.live_safety_allowed_accounts
        if not allowed:
            return None    # empty allowlist = "all accounts allowed"
        if self.account_login not in allowed:
            return (f"account_login={self.account_login} not in "
                    f"allowed_accounts={list(allowed)}")
        return None

    def _check_daily_loss_cap(self) -> Optional[str]:
        pct = self.risk_tracker.account_daily_loss_pct()
        cap = self.risk_config.daily_loss_cap_pct
        if pct >= cap:
            return f"daily_loss_pct={pct:.2f}% >= cap {cap:.2f}%"
        return None

    def _check_risk_engine(self, symbol: str, strategy: str,
                           now_utc: datetime) -> Optional[str]:
        d = self.risk_tracker.pre_trade_check(symbol=symbol, strategy=strategy,
                                               now_utc=now_utc)
        if not d.allowed:
            return d.reason
        return None

    def _check_parity_recent(self, strategy: str,
                             now_utc: datetime) -> Optional[str]:
        if self.risk_config.live_safety_override_parity_recency:
            return None    # explicit override
        if self.parity_gate.is_recent(strategy,
                                       max_age_hours=self.parity_max_age_hours,
                                       now_utc=now_utc):
            return None
        return f"replay-parity for {strategy!r} not recent (>{self.parity_max_age_hours}h)"

    def _check_idempotency(self, idempotency_key: str,
                           now_unix: float) -> Optional[str]:
        # Garbage-collect old keys before checking
        cutoff = now_unix - self.seen_window_seconds
        self._seen_keys = {k: t for k, t in self._seen_keys.items() if t > cutoff}
        if idempotency_key in self._seen_keys:
            seen_ago = now_unix - self._seen_keys[idempotency_key]
            return (f"idempotency_key {idempotency_key!r} seen {seen_ago:.0f}s ago "
                    f"(within {self.seen_window_seconds:.0f}s dedup window)")
        return None

    def _check_no_entry_window(self, now_utc: datetime) -> Optional[str]:
        if self.time_guard_cfg.no_entry_minutes_before_close <= 0:
            return None
        if in_no_entry_window(now_utc, self.time_guard_cfg):
            return ("inside no-entry window before US close "
                    f"({self.time_guard_cfg.no_entry_minutes_before_close} min)")
        return None

    # --- main API ---

    def send_order(self, *, symbol: str, direction: str, lots: float,
                    sl: float, tp: float, deviation: int = 10,
                    comment: str = "",
                    strategy: str,
                    idempotency_key: str,
                    now_utc: Optional[datetime] = None) -> LiveOrder:
        """Run all 7 pre-flight checks; raise SafetyCheckRejection on the
        first failure. On pass, calls bridge_call('order_send', ...) and
        returns a LiveOrder. The bridge call may itself fail — that bubbles
        up as the bridge_call's native error type."""
        now = now_utc or datetime.now(timezone.utc)
        now_unix = now.timestamp()
        checks = [
            ("emergency_stop", self._check_emergency_stop()),
            ("account_allowed", self._check_account_allowed()),
            ("daily_loss_cap", self._check_daily_loss_cap()),
            ("risk_engine", self._check_risk_engine(symbol, strategy, now)),
            ("parity_recent", self._check_parity_recent(strategy, now)),
            ("idempotency", self._check_idempotency(idempotency_key, now_unix)),
            ("no_entry_window", self._check_no_entry_window(now)),
        ]
        for check_id, reason in checks:
            if reason is not None:
                self._log_refusal(check_id, reason, idempotency_key, now)
                raise SafetyCheckRejection(check_id, reason)

        # All checks passed — record the key BEFORE sending so a retry inside
        # the bridge call window is deduped even if the bridge times out.
        self._seen_keys[idempotency_key] = now_unix

        if self._call is None:
            raise RuntimeError(
                "no bridge_call configured; LiveExecutor cannot send orders. "
                "Pass bridge_call=fn to the constructor."
            )
        params = {
            "symbol": symbol, "direction": direction, "lots": lots,
            "sl": sl, "tp": tp, "deviation": deviation,
            "comment": comment, "magic": 999_001,
            "idempotency_key": idempotency_key,
        }
        t0 = time.time()
        try:
            resp = self._call("order_send", params)
            latency_ms = int((time.time() - t0) * 1000)
            ok = isinstance(resp, dict) and resp.get("ok", True) is not False
            storage.record_bridge_event(
                self.db_path, now.isoformat(), "order_send",
                ok, latency_ms, None if ok else str(resp.get("error", resp)),
            )
            ticket = int(resp.get("ticket") or resp.get("data", {}).get("ticket") or 0)
            return LiveOrder(
                ticket=ticket, symbol=symbol, direction=direction, lots=lots,
                sl=sl, tp=tp, sent_at_utc=now.isoformat(),
                idempotency_key=idempotency_key, bridge_response=resp,
            )
        except Exception as e:
            latency_ms = int((time.time() - t0) * 1000)
            storage.record_bridge_event(
                self.db_path, now.isoformat(), "order_send",
                False, latency_ms, f"{type(e).__name__}: {e}",
            )
            raise

    def close_position(self, *, ticket: int, comment: str = "",
                        now_utc: Optional[datetime] = None) -> CloseResult:
        """Close an open position. EMERGENCY_STOP and account-allowed are
        re-checked, but no_entry_window does NOT block closes (closes are
        always allowed; that's how forced-flats reach the broker)."""
        now = now_utc or datetime.now(timezone.utc)
        for cid, reason in (
            ("emergency_stop", self._check_emergency_stop()),
            ("account_allowed", self._check_account_allowed()),
        ):
            if reason is not None:
                self._log_refusal(cid, reason, f"close:{ticket}", now)
                raise SafetyCheckRejection(cid, reason)

        if self._call is None:
            raise RuntimeError("no bridge_call configured")

        t0 = time.time()
        try:
            resp = self._call("order_close", {"ticket": ticket, "comment": comment})
            latency_ms = int((time.time() - t0) * 1000)
            ok = isinstance(resp, dict) and resp.get("ok", True) is not False
            storage.record_bridge_event(
                self.db_path, now.isoformat(), "order_close", ok,
                latency_ms, None if ok else str(resp.get("error", resp)),
            )
            return CloseResult(
                ticket=ticket, closed_at_utc=now.isoformat(),
                reason=comment, bridge_response=resp,
            )
        except Exception as e:
            latency_ms = int((time.time() - t0) * 1000)
            storage.record_bridge_event(
                self.db_path, now.isoformat(), "order_close",
                False, latency_ms, f"{type(e).__name__}: {e}",
            )
            raise

    # --- helpers ---

    def _log_refusal(self, check_id: str, reason: str, key: str,
                     when_utc: datetime) -> None:
        storage.record_bridge_event(
            self.db_path, when_utc.isoformat(), f"preflight_deny:{check_id}",
            False, 0, f"{reason} (key={key})",
        )
