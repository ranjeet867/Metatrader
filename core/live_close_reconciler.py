"""
live_close_reconciler.py — detect and persist server-side TP/SL closes.

Bug C (shipped 2026-05-08):

When the broker auto-closes a bot-opened position via server-side TP/SL,
the position simply vanishes from `positions_get()` on the next runner
tick. Pre-fix the runner had no reconciliation logic for this:

  • No row was written to the `trades` table for the close
  • No `close:tp` / `close:sl` row was written to `bridge_events`
  • `circuit_breaker.evaluate()` (which reads `trades` for realised PnL,
    consec losses, daily loss %) stayed BLIND to the realised P&L of
    every server-side close
  • The edge-decay autolearner (which reads `edge_decay_samples` written
    by `_persist_closed_trade`) couldn't detect strategy erosion
  • Trade Journal / Performance / Live pages all showed empty live tabs
  • The 30-day paper→live promotion gates couldn't accumulate `n_live`
    samples
  • Equity curve diverged from broker equity

Same data-flow gap that Bug B caused on the persistence side — but on
the close *detection* side. Bug B fix made writes work; Bug C fix makes
sure those writes get triggered for every kind of close.

Architecture:

  • DeploymentRunner tracks `_open_live_tickets: dict[int, dict]`
    populated by the open path (after broker verifies the ticket).
  • Each tick, after `_open_positions_snapshot()`, the runner asks this
    module to compare the previous-tick ticket set with the current one.
    Any vanished ticket is reconciled by:
      1. Querying the broker's deal history via `history_deals_get`
      2. Locating the OUT deal that matches the vanished ticket's
         position_id (or ticket directly)
      3. Computing realised PnL = profit + swap + commission
      4. Computing R multiple from entry, exit, stop
      5. Inferring close_reason (tp / sl / manual / forced_flat)
      6. Calling `_persist_closed_trade(d, closed)` — which now writes
         a row to `trades` and records a `close:{reason}` bridge_event

  • Backfill: scripts/reconcile_missing_live_closes.py uses the same
    logic to recover any historical closes that happened before the
    runner started tracking tickets.

Idempotent — a closed ticket already present in `trades` (matched by
idempotency_key) is skipped on subsequent reconciliation runs.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# Magic number the bot uses on its order_send. Phantom positions opened
# manually in MT5 use magic=0 and are never reconciled by this module
# (they aren't bot trades).
BOT_MAGIC = 999_001


@dataclass
class LiveOpenContext:
    """Per-ticket context captured when the bot opens a position. Used
    later by the reconciler to attribute the close to the right
    deployment and compute R-multiple from the original stop level."""
    ticket: int
    deployment_id: str
    symbol: str
    tf: str
    strategy: str
    direction: str    # 'LONG' | 'SHORT'
    entry_price: float
    stop_price: float
    target_price: float
    lots: float
    opened_at_utc: str

    def to_dict(self) -> dict:
        return {
            "ticket": self.ticket,
            "deployment_id": self.deployment_id,
            "symbol": self.symbol,
            "tf": self.tf,
            "strategy": self.strategy,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "lots": self.lots,
            "opened_at_utc": self.opened_at_utc,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LiveOpenContext":
        return cls(
            ticket=int(d["ticket"]),
            deployment_id=str(d["deployment_id"]),
            symbol=str(d["symbol"]),
            tf=str(d["tf"]),
            strategy=str(d["strategy"]),
            direction=str(d["direction"]),
            entry_price=float(d["entry_price"]),
            stop_price=float(d["stop_price"]),
            target_price=float(d["target_price"]),
            lots=float(d["lots"]),
            opened_at_utc=str(d["opened_at_utc"]),
        )


def infer_close_reason(deal_comment: str, profit: float) -> str:
    """Heuristic: MT5 stamps deal comments with tags when TP/SL fires
    server-side. Common patterns:
      "[tp]", "[sl]", "tp", "sl"   — server-side TP/SL
      "expert close" / ""           — manual or programmatic close
    Fall back to profit sign when comment is unhelpful."""
    c = (deal_comment or "").lower()
    if "[tp]" in c or c.strip() in ("tp", "tp1", "tp2"):
        return "tp"
    if "[sl]" in c or c.strip() in ("sl", "stop"):
        return "sl"
    if "manual" in c:
        return "manual"
    if "forced" in c or "flat" in c or "weekend" in c:
        return "forced_flat"
    # Fall back to sign — informative even if not the platform's literal
    # close-reason. Prefer a winning close to be tagged "tp_or_close" so
    # downstream queries can group winners.
    return "tp_or_close" if profit >= 0 else "sl_or_close"


def compute_r_multiple(direction: str, entry: float, exit_price: float,
                         stop: float) -> float:
    """R multiple = (exit - entry) / (entry - stop) for LONG, sign-flipped
    for SHORT. Returns 0.0 if stop_distance is zero (degenerate)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    if direction.upper() == "LONG":
        pnl_in_price = exit_price - entry
    else:
        pnl_in_price = entry - exit_price
    return pnl_in_price / risk


def find_close_deal(deals: list, ticket: int) -> Optional[object]:
    """Locate the OUT deal in a `history_deals_get` response that closes
    a given ticket. MT5 deals have:
      entry == 0 → IN  (open / increase)
      entry == 1 → OUT (close / decrease)
    Match by ticket OR position_id (broker conventions vary).
    Returns the latest matching OUT deal."""
    candidates = []
    for d in deals:
        if int(getattr(d, "entry", 0)) != 1:    # only OUT deals
            continue
        # Match: ticket equality OR position_id equality. The bot opens
        # with one ticket and the broker may report the close under a
        # different ticket number whose position_id matches the open
        # ticket — both are valid links.
        d_ticket = int(getattr(d, "ticket", 0) or 0)
        d_position_id = int(getattr(d, "position_id", 0) or 0)
        if d_ticket == ticket or d_position_id == ticket:
            candidates.append(d)
    if not candidates:
        return None
    # If multiple OUT deals share the position (e.g. partial closes),
    # prefer the one with the latest time_utc.
    return max(candidates, key=lambda d: getattr(d, "time_utc", ""))


def find_open_deal(deals: list, ticket: int) -> Optional[object]:
    """Locate the IN deal that opened a ticket. Symmetric to
    find_close_deal but for entry==0."""
    candidates = []
    for d in deals:
        if int(getattr(d, "entry", 0)) != 0:
            continue
        d_ticket = int(getattr(d, "ticket", 0) or 0)
        d_position_id = int(getattr(d, "position_id", 0) or 0)
        if d_ticket == ticket or d_position_id == ticket:
            candidates.append(d)
    if not candidates:
        return None
    return min(candidates, key=lambda d: getattr(d, "time_utc", ""))


def build_closed_record(
    *,
    ctx: LiveOpenContext,
    out_deal: object,
    in_deal: Optional[object] = None,
) -> SimpleNamespace:
    """Build the synthetic ClosedPosition-like object that
    `DeploymentRunner._persist_closed_trade` expects.

    Required attrs on the returned object:
      direction, entry_price, stop_price, target_price, exit_price,
      lots, realized_pnl, r_multiple, close_reason,
      opened_at_utc, closed_at_utc, idempotency_key

    `ctx` provides entry_price / stop_price / target_price / direction —
    these were captured on the OPEN side by the runner, so they're the
    canonical values regardless of how the broker formats its history.
    `out_deal` provides exit_price / pnl / time. `in_deal` is optional —
    used to refine entry_price if the bot context's price is missing
    (shouldn't happen but defensive)."""
    profit = float(getattr(out_deal, "profit", 0.0) or 0.0)
    swap = float(getattr(out_deal, "swap", 0.0) or 0.0)
    commission = float(getattr(out_deal, "commission", 0.0) or 0.0)
    realized_pnl = profit + swap + commission

    exit_price = float(getattr(out_deal, "price", 0.0) or 0.0)
    if exit_price <= 0:
        # Defensive: if broker returned 0 (rare), fall back to ctx target
        # so we at least have a reasonable proxy. Won't exactly match
        # broker but prevents NULL exit_price violating the CHECK.
        exit_price = ctx.target_price

    entry_price = ctx.entry_price
    if in_deal is not None:
        in_price = float(getattr(in_deal, "price", 0.0) or 0.0)
        if in_price > 0:
            entry_price = in_price

    closed_at_utc = str(getattr(out_deal, "time_utc", ""))
    if not closed_at_utc:
        closed_at_utc = datetime.now(timezone.utc).isoformat()

    reason = infer_close_reason(
        deal_comment=str(getattr(out_deal, "comment", "")),
        profit=realized_pnl,
    )

    r_mult = compute_r_multiple(
        direction=ctx.direction,
        entry=entry_price,
        exit_price=exit_price,
        stop=ctx.stop_price,
    )

    # Idempotency key — uses ticket so re-running reconciliation never
    # double-inserts the same close. Different from open's key (which
    # is per-bar) on purpose.
    idem_key = f"close:{ctx.ticket}"

    return SimpleNamespace(
        direction=ctx.direction,
        entry_price=entry_price,
        stop_price=ctx.stop_price,
        target_price=ctx.target_price,
        exit_price=exit_price,
        lots=ctx.lots,
        realized_pnl=realized_pnl,
        r_multiple=r_mult,
        close_reason=reason,
        opened_at_utc=ctx.opened_at_utc,
        closed_at_utc=closed_at_utc,
        idempotency_key=idem_key,
    )


def already_persisted(db_path: str | Path, ticket: int) -> bool:
    """Idempotency check — returns True if a row in `trades` already
    has the close-key for this ticket. Used by the backfill script
    AND by the runtime reconciler to avoid double-inserts on retries."""
    key = f"close:{ticket}"
    try:
        with sqlite3.connect(str(db_path)) as c:
            row = c.execute(
                "SELECT 1 FROM trades WHERE idempotency_key = ? LIMIT 1",
                (key,),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def reconcile_vanished_tickets(
    *,
    open_contexts: dict[int, LiveOpenContext],
    current_bot_tickets: set[int],
    bridge_call,
    db_path: str | Path,
    persist_fn,
    record_event_fn,
    deployment_lookup_fn,
    history_window_hours: float = 48.0,
) -> dict:
    """Compare `open_contexts` (last-tick state) with `current_bot_tickets`
    (broker right now), and reconcile every vanished ticket.

    Args:
        open_contexts: ticket → LiveOpenContext from the last tick.
        current_bot_tickets: tickets we still see on the broker (filtered
            to magic == BOT_MAGIC by the caller).
        bridge_call: the runner's bridge callable, used to invoke
            history_deals_get.
        db_path: account DB path for idempotency check.
        persist_fn: callable taking (d, closed) — typically
            `DeploymentRunner._persist_closed_trade`.
        record_event_fn: callable taking (db_path, iso_now, method, ok,
            latency_ms, error) — typically `storage.record_bridge_event`.
        deployment_lookup_fn: callable(deployment_id) → Deployment, used
            to find the parent deployment for the ticket's persist call.
        history_window_hours: how far back to query deal history. 48h
            covers the worst-case "weekend then Monday" window where
            a Friday open hit TP on Monday.

    Returns:
        dict with reconciliation summary:
            reconciled: list of {ticket, pnl, reason}
            skipped_already_persisted: list of tickets
            unmatched: list of tickets where no OUT deal was found
            errors: list of {ticket, error}
    """
    out = {"reconciled": [], "skipped_already_persisted": [],
           "unmatched": [], "errors": []}

    vanished = set(open_contexts.keys()) - current_bot_tickets
    if not vanished:
        return out

    # Pull deal history once; covers all vanished tickets
    try:
        from core.mt5_account import MT5AccountClient
        client = MT5AccountClient(bridge_call=bridge_call)
        since = datetime.now(timezone.utc) - timedelta(hours=history_window_hours)
        deals = client.history_deals_get(since)
    except Exception as e:
        log.exception("history_deals_get failed in reconciler")
        for t in vanished:
            out["errors"].append({"ticket": t, "error": f"history_fetch: {e}"})
        return out

    for ticket in sorted(vanished):
        ctx = open_contexts.get(ticket)
        if ctx is None:
            continue
        # Skip if already in trades (defensive; backfill could have
        # already covered this on a prior run)
        if already_persisted(db_path, ticket):
            out["skipped_already_persisted"].append(ticket)
            continue
        out_deal = find_close_deal(deals, ticket)
        if out_deal is None:
            log.warning(
                "reconciler: no OUT deal found for ticket %d in %dh "
                "history window — leaving in open_contexts for retry",
                ticket, int(history_window_hours),
            )
            out["unmatched"].append(ticket)
            continue
        in_deal = find_open_deal(deals, ticket)
        try:
            d = deployment_lookup_fn(ctx.deployment_id)
            if d is None:
                # Deployment was removed — synthesize a stub so we can
                # still write the trade (we'd rather have the row with
                # an orphan run_id than lose the data)
                d = SimpleNamespace(
                    deployment_id=ctx.deployment_id,
                    ticker=ctx.symbol, tf=ctx.tf, strategy=ctx.strategy,
                    status="live",
                )
            closed = build_closed_record(
                ctx=ctx, out_deal=out_deal, in_deal=in_deal,
            )
            persist_fn(d, closed)
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                record_event_fn(
                    db_path, now_iso,
                    method=f"close:{closed.close_reason}",
                    ok=True, latency_ms=0,
                    error=(f"ticket={ticket} pnl={closed.realized_pnl:+.2f} "
                           f"R={closed.r_multiple:+.2f} "
                           f"deployment={ctx.deployment_id}"),
                )
            except Exception:
                log.debug("record_bridge_event close failed (non-fatal)",
                          exc_info=True)
            out["reconciled"].append({
                "ticket": ticket,
                "pnl": closed.realized_pnl,
                "reason": closed.close_reason,
                "r_multiple": closed.r_multiple,
                "deployment_id": ctx.deployment_id,
            })
            log.info(
                "✅ LIVE CLOSE reconciled  ticket=%d  dep=%s  "
                "pnl=%+.2f  R=%+.2f  reason=%s",
                ticket, ctx.deployment_id,
                closed.realized_pnl, closed.r_multiple, closed.close_reason,
            )
        except Exception as e:
            log.exception("reconciler: persist failed for ticket %d", ticket)
            out["errors"].append({"ticket": ticket, "error": str(e)})

    return out
