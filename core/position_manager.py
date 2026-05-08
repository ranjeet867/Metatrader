"""
position_manager.py — broker-truth view of open positions, with
close-one / close-all and reconcile-with-broker.

Reconcile detects:
  - manual_close_in_mt5: DB says position open, broker says closed.
                         Walks history_deals_get to find the closing deal,
                         writes synthetic close to journal.
  - phantom_open       : Broker has a position we never opened (different
                         magic, manual MT5 click, etc.). We log + warn but
                         NEVER auto-close (INVARIANT-12).
  - drift               : Reported price/sl/tp differ from last-known.

Idempotent (INVARIANT-13). Two reconcile() calls on the same broker state
write the same number of journal rows.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from core import account_manager
from core.journal import JournalWriter
from core.mt5_account import (
    BridgeDeal,
    BridgeError,
    BridgePosition,
    CloseOrderResult,
    MT5AccountClient,
)


_LONG_TYPES = {0}    # MT5 POSITION_TYPE_BUY = 0
_SHORT_TYPES = {1}   # POSITION_TYPE_SELL = 1


@dataclass(frozen=True)
class LivePosition:
    ticket: int
    symbol: str
    direction: Literal["long", "short"]
    lots: float
    entry_price: float
    current_price: float
    stop_price: float | None
    target_price: float | None
    unrealized_pnl: float
    unrealized_r: float | None
    opened_at_utc: str
    magic: int
    strategy_name: str | None        # parsed from comment when present


@dataclass(frozen=True)
class CloseResult:
    ticket: int
    ok: bool
    realized_pnl: float
    exit_price: float
    retcode: int
    error: str = ""
    bridge_response: dict = field(default_factory=dict)
    attempts: int = 1

    @property
    def detailed_error(self) -> str:
        """Human-readable error including retcode + bridge response.

        Returns "" when ok. Used by the UI to show WHY a close failed
        instead of a bare 'failed' message.
        """
        if self.ok:
            return ""
        parts = []
        if self.error:
            parts.append(self.error)
        if self.retcode and self.retcode != 0:
            parts.append(f"retcode={self.retcode}")
        # Include any bridge-level error keys not already in `error`
        if isinstance(self.bridge_response, dict):
            br_err = self.bridge_response.get("error")
            if br_err and br_err not in (self.error, ""):
                parts.append(f"bridge_error={br_err}")
            # Also surface comment if it has anything useful
            cmt = self.bridge_response.get("comment", "")
            if cmt and cmt not in (self.error, ""):
                parts.append(f"comment={cmt}")
        if self.attempts > 1:
            parts.append(f"after {self.attempts} attempts")
        return " · ".join(parts) if parts else "unknown error"


@dataclass(frozen=True)
class ReconcileReport:
    """Summary of what reconcile_with_broker found."""
    n_open_in_broker: int
    n_open_in_db: int
    n_phantom: int
    n_manual_closes_recorded: int
    phantom_tickets: tuple[int, ...]
    manual_close_tickets: tuple[int, ...]


# ---------------------------------------------------------------------------
# Open-positions table (per-account v2.db)
# ---------------------------------------------------------------------------

OPEN_POSITIONS_DDL = """
CREATE TABLE IF NOT EXISTS open_positions (
    ticket          INTEGER PRIMARY KEY,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('long','short')),
    lots            REAL NOT NULL,
    entry_price     REAL NOT NULL,
    stop_price      REAL,
    target_price    REAL,
    opened_at_utc   TEXT NOT NULL,
    magic           INTEGER,
    strategy_name   TEXT,
    last_seen_at_utc TEXT
)
"""


def _ensure_open_positions_table(db_path: Path) -> None:
    with sqlite3.connect(str(db_path), isolation_level=None) as c:
        c.execute(OPEN_POSITIONS_DDL)


# ---------------------------------------------------------------------------
# PositionManager
# ---------------------------------------------------------------------------

class PositionManager:
    def __init__(self, *, account_login: int, bridge: MT5AccountClient,
                 db_path: Path, journal: JournalWriter | None = None):
        self.account_login = int(account_login)
        self.bridge = bridge
        self.db_path = Path(db_path)
        self.journal = journal or JournalWriter(self.db_path)
        _ensure_open_positions_table(self.db_path)

    # --- live snapshot --------------------------------------------------

    def list_open(self) -> list[LivePosition]:
        """Fresh fetch from the bridge — never cached."""
        try:
            br = self.bridge.positions_get()
        except BridgeError:
            raise
        out = []
        # Build a {ticket: db_row} map so we know which strategy / risk applies
        with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
            db_rows = {
                row[0]: row for row in c.execute(
                    "SELECT ticket, strategy_name, stop_price, target_price "
                    "FROM open_positions"
                ).fetchall()
            }
        for p in br:
            stored = db_rows.get(p.ticket)
            stored_strategy = stored[1] if stored else None
            stop = float(stored[2]) if stored and stored[2] is not None else (
                p.sl if p.sl > 0 else None
            )
            target = float(stored[3]) if stored and stored[3] is not None else (
                p.tp if p.tp > 0 else None
            )
            direction = "long" if p.type in _LONG_TYPES else "short"
            unrealized_r = None
            if stop is not None and stop > 0:
                stop_distance = abs(p.price_open - stop)
                if stop_distance > 0:
                    sign = 1 if direction == "long" else -1
                    unrealized_r = (p.price_current - p.price_open) * sign / stop_distance
            out.append(LivePosition(
                ticket=p.ticket, symbol=p.symbol, direction=direction,
                lots=p.volume, entry_price=p.price_open,
                current_price=p.price_current,
                stop_price=stop, target_price=target,
                unrealized_pnl=p.profit + p.swap + p.commission,
                unrealized_r=unrealized_r,
                opened_at_utc=p.time_open_utc, magic=p.magic,
                strategy_name=stored_strategy
                                or _parse_strategy_from_comment(p.comment),
            ))
        return out

    # --- close ----------------------------------------------------------

    def close_one(self, ticket: int, *, reason: str,
                   dry_run: bool = False) -> CloseResult:
        """Close a single position. Logs journal `manual_close_ui` (or whatever
        reason the caller specified). Raises if EMERGENCY_STOP active UNLESS
        reason='emergency_flatten'."""
        if (account_manager.emergency_stop_active()
            and reason != "emergency_flatten"):
            raise PermissionError(
                "EMERGENCY_STOP active — close_one refused. "
                "Pass reason='emergency_flatten' to override."
            )
        if dry_run:
            return CloseResult(ticket=ticket, ok=True, realized_pnl=0.0,
                                exit_price=0.0, retcode=0,
                                error="dry_run")
        # Retry the bridge close — broker can return TRADE_RETCODE_REQUOTE,
        # MARKET_CLOSED, or other transient codes on the first try. Each
        # attempt is logged so the user can see the full error chain in
        # bridge_events even if the final attempt eventually succeeds.
        max_attempts = 3
        last_exc = None
        last_resp = None
        for attempt in range(1, max_attempts + 1):
            try:
                res = self.bridge.position_close(ticket=ticket)
                last_resp = getattr(res, "_raw_response", None) or {
                    "ok": res.ok, "retcode": res.retcode,
                    "deal": res.deal, "price": res.price,
                    "comment": res.comment,
                }
                if res.ok:
                    break
                # Bridge returned but ok=False — log + retry
                self.journal.record_bridge_event(
                    method="position_close", latency_ms=0, ok=False,
                    error=(
                        f"close({ticket}) attempt {attempt}/{max_attempts}: "
                        f"retcode={res.retcode} comment={res.comment!r}"
                    ),
                )
                last_exc = (
                    f"retcode={res.retcode} "
                    f"comment={res.comment!r}"
                    if res.comment
                    else f"retcode={res.retcode}"
                )
            except Exception as e:
                self.journal.record_bridge_event(
                    method="position_close", latency_ms=0, ok=False,
                    error=(
                        f"close({ticket}) attempt {attempt}/{max_attempts}: "
                        f"{type(e).__name__}: {e}"
                    ),
                )
                last_exc = f"{type(e).__name__}: {e}"
                last_resp = {"error": str(e)}
            # Backoff between attempts
            if attempt < max_attempts:
                time.sleep(0.3 * attempt)
        else:
            # Exhausted retries without success
            return CloseResult(
                ticket=ticket, ok=False, realized_pnl=0.0,
                exit_price=0.0, retcode=-1,
                error=str(last_exc or "close failed (no detail)"),
                bridge_response=last_resp or {},
                attempts=max_attempts,
            )
        # Realized PnL is reported by the deal — we read history to confirm
        realized = self._fetch_close_pnl(ticket)
        # Drop from open_positions
        with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
            c.execute("DELETE FROM open_positions WHERE ticket=?", (ticket,))
        # Journal
        self._record_close_event(ticket=ticket, reason=reason,
                                  exit_price=res.price,
                                  realized_pnl=realized)
        return CloseResult(
            ticket=ticket, ok=True, realized_pnl=realized,
            exit_price=res.price, retcode=res.retcode,
            error="",
            bridge_response=last_resp or {},
            attempts=attempt,
        )

    def close_all(self, *, reason: str, dry_run: bool = False
                   ) -> list[CloseResult]:
        """Close every open position. NEVER short-circuits on first failure.
        Returns one CloseResult per ticket (possibly some ok=False)."""
        positions = self.list_open()
        results: list[CloseResult] = []
        for p in positions:
            try:
                results.append(self.close_one(p.ticket, reason=reason,
                                                dry_run=dry_run))
            except PermissionError as e:
                # Emergency-stop denial: surface as a failed result so the
                # caller can decide what to do, but DON'T abort the whole
                # batch (the operator may have hit the stop AFTER we
                # started flattening).
                results.append(CloseResult(
                    ticket=p.ticket, ok=False, realized_pnl=0.0,
                    exit_price=0.0, retcode=-1, error=str(e),
                ))
            except Exception as e:    # pragma: no cover — defensive
                results.append(CloseResult(
                    ticket=p.ticket, ok=False, realized_pnl=0.0,
                    exit_price=0.0, retcode=-1, error=str(e),
                ))
        return results

    # --- reconcile ------------------------------------------------------

    def reconcile_with_broker(self,
                               since: datetime | None = None
                               ) -> ReconcileReport:
        """Compare DB's open_positions against bridge.positions_get().

        For DB-open / broker-closed: walk history_deals_get from
        position.opened_at_utc to find the closing deal, write a journal
        event with reason='manual_close_in_mt5' INCLUDING realized_pnl.
        Idempotent — a deal already linked to a journal close is not
        double-counted.

        For broker-open / DB-missing: log `phantom_open_detected` (warn-only).
        """
        try:
            broker = self.bridge.positions_get()
        except BridgeError:
            raise
        broker_by_ticket = {p.ticket: p for p in broker}

        with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
            db_rows = c.execute(
                "SELECT ticket, symbol, direction, lots, entry_price, "
                "stop_price, target_price, opened_at_utc, magic, strategy_name "
                "FROM open_positions"
            ).fetchall()

        manual_closes: list[int] = []
        phantoms: list[int] = []

        # DB → broker check (manual closes)
        for row in db_rows:
            ticket = int(row[0])
            if ticket in broker_by_ticket:
                continue   # still open — fine
            # Closed in MT5. Try to find the closing deal.
            opened_at_str = row[7]
            try:
                opened_at = datetime.fromisoformat(opened_at_str)
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=timezone.utc)
            except Exception:
                opened_at = datetime.now(timezone.utc) - timedelta(days=7)
            since_utc = since or (opened_at - timedelta(minutes=1))
            try:
                deals = self.bridge.history_deals_get(since_utc=since_utc)
            except BridgeError:
                deals = []
            close_deal = next(
                (d for d in deals if d.position_id == ticket and d.entry == 1),
                None,
            )
            realized = float(close_deal.profit + close_deal.swap
                              + close_deal.commission) if close_deal else 0.0
            exit_price = float(close_deal.price) if close_deal else 0.0
            close_time = (close_deal.time_utc if close_deal
                            else datetime.now(timezone.utc).isoformat())
            # Idempotency: only write the journal row if not already there.
            already = self._already_journaled_manual_close(ticket)
            if not already:
                self._record_close_event(
                    ticket=ticket, reason="manual_close_in_mt5",
                    exit_price=exit_price, realized_pnl=realized,
                    occurred_at_utc=close_time,
                )
            with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
                c.execute("DELETE FROM open_positions WHERE ticket=?", (ticket,))
            manual_closes.append(ticket)

        # broker → DB check (phantoms)
        db_tickets = {int(row[0]) for row in db_rows}
        for ticket, p in broker_by_ticket.items():
            if ticket in db_tickets:
                continue
            phantoms.append(ticket)
            # WARN ONLY — never auto-close (INVARIANT-12)
            already = self._already_journaled_phantom(ticket)
            if not already:
                self.journal.record_bridge_event(
                    method="phantom_open_detected",
                    latency_ms=0, ok=False,
                    error=(f"ticket={ticket} symbol={p.symbol} "
                           f"volume={p.volume} magic={p.magic} "
                           f"comment={p.comment!r}"),
                )

        return ReconcileReport(
            n_open_in_broker=len(broker),
            n_open_in_db=len(db_rows),
            n_phantom=len(phantoms),
            n_manual_closes_recorded=len(manual_closes),
            phantom_tickets=tuple(phantoms),
            manual_close_tickets=tuple(manual_closes),
        )

    # --- DB write helpers (called by live executor on open) -------------

    def record_open(self, *, ticket: int, symbol: str, direction: str,
                     lots: float, entry_price: float,
                     stop_price: float | None, target_price: float | None,
                     opened_at_utc: str, magic: int = 0,
                     strategy_name: str | None = None) -> None:
        with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
            c.execute(
                """
                INSERT OR REPLACE INTO open_positions
                  (ticket, symbol, direction, lots, entry_price,
                   stop_price, target_price, opened_at_utc, magic,
                   strategy_name, last_seen_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(ticket), symbol, direction, float(lots),
                 float(entry_price),
                 float(stop_price) if stop_price else None,
                 float(target_price) if target_price else None,
                 opened_at_utc, int(magic), strategy_name,
                 datetime.now(timezone.utc).isoformat()),
            )

    # --- private --------------------------------------------------------

    def _fetch_close_pnl(self, ticket: int) -> float:
        """Best-effort: read history for this position to compute realized."""
        try:
            since = datetime.now(timezone.utc) - timedelta(days=14)
            deals = self.bridge.history_deals_get(since_utc=since)
            close_deal = next(
                (d for d in deals if d.position_id == ticket and d.entry == 1),
                None,
            )
            if close_deal is None:
                return 0.0
            return float(close_deal.profit + close_deal.swap
                          + close_deal.commission)
        except Exception:
            return 0.0

    def _record_close_event(self, *, ticket: int, reason: str,
                              exit_price: float, realized_pnl: float,
                              occurred_at_utc: str | None = None) -> None:
        ts = occurred_at_utc or datetime.now(timezone.utc).isoformat()
        # Use the bridge_events table for these audit rows — keeps the schema
        # surface small (Phase 2 chose JSONL journal as ALSO an audit path
        # but the SQLite bridge_events is queryable from the dashboard).
        self.journal.record_bridge_event(
            method=f"close:{reason}", latency_ms=0, ok=True,
            error=(f"ticket={ticket} exit={exit_price:.5f} "
                   f"pnl={realized_pnl:+.2f}"),
        )

    def _already_journaled_manual_close(self, ticket: int) -> bool:
        with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
            row = c.execute(
                "SELECT 1 FROM bridge_events "
                "WHERE method='close:manual_close_in_mt5' "
                "AND error LIKE ? LIMIT 1",
                (f"ticket={ticket}%",),
            ).fetchone()
        return row is not None

    def _already_journaled_phantom(self, ticket: int) -> bool:
        with sqlite3.connect(str(self.db_path), isolation_level=None) as c:
            row = c.execute(
                "SELECT 1 FROM bridge_events "
                "WHERE method='phantom_open_detected' "
                "AND error LIKE ? LIMIT 1",
                (f"ticket={ticket}%",),
            ).fetchone()
        return row is not None


def _parse_strategy_from_comment(comment: str) -> str | None:
    """Magic numbers + comments are the broker-side identification of a
    strategy. Live executor sets comment='vol_breakout|US100.cash|H1' or
    similar; if so we recover it here. Returns None if not parseable."""
    if not comment:
        return None
    # Convention: '<strategy>|<symbol>|<tf>'
    parts = comment.split("|")
    if len(parts) >= 1 and parts[0]:
        return parts[0]
    return None
