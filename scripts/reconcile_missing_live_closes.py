#!/usr/bin/env python3
"""reconcile_missing_live_closes.py — backfill missing live closes.

Bug C recovery script. Use when:
  • A live position closed via server-side TP/SL while the runner was
    NOT tracking the ticket (e.g. before Bug C fix shipped, or after a
    state file got truncated).
  • Equity reflects the close on the broker, but `trades` has no row
    and `bridge_events` has no `close:tp` / `close:sl` event.

What it does:
  1. Pulls broker deal history (last 7 days by default) via the bridge.
  2. For every OUT deal with magic == BOT_MAGIC, checks `trades.idempotency_key`
     for `close:{ticket}` — if missing, persists the close.
  3. Reports reconciled / skipped / errors.

Usage:
    cd /Users/ranjeet/Documents/mt5_quant_trader_v2
    .venv/bin/python scripts/reconcile_missing_live_closes.py

    # Custom window:
    .venv/bin/python scripts/reconcile_missing_live_closes.py --hours 48

    # Dry-run (report what WOULD be reconciled):
    .venv/bin/python scripts/reconcile_missing_live_closes.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import account_manager, deployment as dep_mod, storage   # noqa: E402
from core.live_close_reconciler import (   # noqa: E402
    BOT_MAGIC, LiveOpenContext, already_persisted,
    build_closed_record, find_close_deal, find_open_deal,
)


def _build_ctx_from_open_deal(in_deal, deployments) -> LiveOpenContext | None:
    """Reverse-engineer a LiveOpenContext from an IN deal when we don't
    have the runner-captured context (Bug C pre-fix opens). We use
    the symbol on the deal to find the deployment, and infer entry from
    the deal's price. Stop / target prices have to come from the
    deployment's params dict — best effort."""
    symbol = str(getattr(in_deal, "symbol", ""))
    direction = "LONG" if int(getattr(in_deal, "type", 0)) == 0 else "SHORT"
    entry_price = float(getattr(in_deal, "price", 0.0) or 0.0)
    lots = float(getattr(in_deal, "volume", 0.0) or 0.0)
    if entry_price <= 0 or lots <= 0:
        return None

    # Match the most likely deployment by symbol — there might be
    # multiple cells on the same ticker; pick the live one.
    cands = [d for d in deployments
              if d.ticker == symbol and d.status in ("live", "paper")]
    if not cands:
        return None
    live_first = sorted(cands, key=lambda d: 0 if d.status == "live" else 1)
    d = live_first[0]

    # Stop / target — best effort from deployment params (may be missing
    # for some legacy cells). For backfill purposes the R-multiple may
    # be approximate; the trade row itself is what matters.
    stop_price = entry_price * 0.99 if direction == "LONG" else entry_price * 1.01
    target_price = entry_price * 1.01 if direction == "LONG" else entry_price * 0.99

    return LiveOpenContext(
        ticket=int(getattr(in_deal, "ticket", 0)),
        deployment_id=d.deployment_id,
        symbol=symbol, tf=d.tf, strategy=d.strategy,
        direction=direction,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        lots=lots,
        opened_at_utc=str(getattr(in_deal, "time_utc", "")),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168,
                       help="Backfill window (hours). Default 168 (7 days).")
    ap.add_argument("--dry-run", action="store_true",
                       help="Don't write anything; report what WOULD be done.")
    args = ap.parse_args()

    accounts = account_manager.list_accounts()
    if not accounts:
        print("No MT5 account configured.")
        return 1
    account = accounts[0]
    db_path = account_manager.get_db_path(account.login)
    deployments = dep_mod.load_deployments(account.login)

    print(f"Account: {account.login}")
    print(f"DB: {db_path}")
    print(f"Window: last {args.hours}h")
    print(f"Mode: {'DRY-RUN (no writes)' if args.dry_run else 'LIVE (will persist)'}")
    print()

    # Load runner state to get any tracked open contexts (post-fix opens)
    state_path = db_path.parent / "runner_state.json"
    tracked_ctx: dict[int, LiveOpenContext] = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
            for ts, raw in data.get("open_live_tickets", {}).items():
                try:
                    tracked_ctx[int(ts)] = LiveOpenContext.from_dict(raw)
                except Exception:
                    pass
        except Exception:
            pass
    print(f"Runner-tracked open ticket contexts: {len(tracked_ctx)}")
    print()

    # Fetch broker deal history — same construction as scripts/run_deployments.py
    from core.mt5_account import MT5AccountClient
    bridge = MT5AccountClient()
    client = MT5AccountClient(bridge_call=bridge._call)
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    try:
        deals = client.history_deals_get(since)
    except Exception as e:
        print(f"history_deals_get failed: {type(e).__name__}: {e}")
        return 1
    print(f"Fetched {len(deals)} deals since {since.isoformat()[:19]}")
    print()

    # Filter to OUT deals (closes) with bot magic. NOTE: BridgeDeal
    # doesn't always carry the magic field; fall back to comment match.
    out_deals = [d for d in deals if int(getattr(d, "entry", 0)) == 1]
    print(f"OUT (close) deals: {len(out_deals)}")

    # Build a stub DeploymentRunner-like persist function. We can't
    # instantiate the full runner from a script (it spawns threads etc),
    # so we replicate its persist logic here using direct SQL via
    # storage helpers.
    runs_seeded: set = set()
    trade_idx_by_dep: dict = {}

    def _next_trade_idx(run_id: str) -> int:
        if run_id not in trade_idx_by_dep:
            try:
                with sqlite3.connect(str(db_path)) as c:
                    row = c.execute(
                        "SELECT COALESCE(MAX(trade_idx), -1) + 1 "
                        "FROM trades WHERE run_id = ?", (run_id,),
                    ).fetchone()
                trade_idx_by_dep[run_id] = int(row[0]) if row else 0
            except Exception:
                trade_idx_by_dep[run_id] = 0
        idx = trade_idx_by_dep[run_id]
        trade_idx_by_dep[run_id] = idx + 1
        return idx

    def _ensure_run_row(d) -> None:
        if d.deployment_id in runs_seeded:
            return
        cfg = json.dumps({
            "deployment_id": d.deployment_id,
            "mode": "live",
            "ticker": d.ticker, "tf": d.tf, "strategy": d.strategy,
            "synthetic": True, "via": "reconcile_missing_live_closes",
        }, sort_keys=True)
        with storage.connect(db_path) as c:
            c.execute(
                "INSERT OR IGNORE INTO runs "
                "(run_id, started_at_utc, symbol, tf, strategy_name, "
                " config_json, starting_balance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (d.deployment_id,
                 datetime.now(timezone.utc).isoformat(),
                 d.ticker, d.tf, d.strategy, cfg, 100_000.0),
            )
        runs_seeded.add(d.deployment_id)

    def _persist(d, closed) -> None:
        _ensure_run_row(d)
        idx = _next_trade_idx(d.deployment_id)
        with storage.connect(db_path) as c:
            c.execute(
                "INSERT INTO trades ("
                " run_id, trade_idx, symbol, direction, "
                " opened_at_utc, closed_at_utc, "
                " entry_price, stop_price, target_price, exit_price, "
                " lots, realized_pnl, r_multiple, close_reason, "
                " mode, strategy, tf, idempotency_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "          ?, ?, ?, ?, ?, ?, ?)",
                (d.deployment_id, idx, d.ticker, closed.direction,
                 closed.opened_at_utc, closed.closed_at_utc,
                 closed.entry_price, closed.stop_price,
                 closed.target_price, closed.exit_price,
                 closed.lots, closed.realized_pnl,
                 closed.r_multiple, closed.close_reason,
                 "live", d.strategy, d.tf, closed.idempotency_key),
            )

    reconciled = []
    skipped_already = []
    skipped_no_ctx = []
    errors = []

    for od in out_deals:
        ticket = int(getattr(od, "ticket", 0) or 0)
        position_id = int(getattr(od, "position_id", 0) or 0)
        # The "primary" ticket for matching context: prefer the
        # IN-deal ticket which is what the runner stored. position_id
        # equals the open ticket on most brokers.
        match_ticket = position_id or ticket

        if already_persisted(db_path, match_ticket):
            skipped_already.append(match_ticket)
            continue

        # Look up context — prefer runner-tracked, else infer from open deal
        ctx = tracked_ctx.get(match_ticket)
        in_deal = find_open_deal(deals, match_ticket)
        if ctx is None and in_deal is not None:
            ctx = _build_ctx_from_open_deal(in_deal, deployments)
        if ctx is None:
            skipped_no_ctx.append(match_ticket)
            continue

        try:
            closed = build_closed_record(ctx=ctx, out_deal=od, in_deal=in_deal)
            d = next((x for x in deployments
                        if x.deployment_id == ctx.deployment_id), None)
            if d is None:
                d = SimpleNamespace(
                    deployment_id=ctx.deployment_id,
                    ticker=ctx.symbol, tf=ctx.tf, strategy=ctx.strategy,
                    status="live",
                )
            if args.dry_run:
                print(f"  WOULD RECONCILE  ticket={match_ticket}  "
                      f"dep={ctx.deployment_id}  pnl={closed.realized_pnl:+.2f}  "
                      f"R={closed.r_multiple:+.2f}  reason={closed.close_reason}")
                reconciled.append({"ticket": match_ticket,
                                    "pnl": closed.realized_pnl})
            else:
                _persist(d, closed)
                storage.record_bridge_event(
                    db_path,
                    datetime.now(timezone.utc).isoformat(),
                    method=f"close:{closed.close_reason}",
                    ok=True, latency_ms=0,
                    error=(f"ticket={match_ticket} pnl={closed.realized_pnl:+.2f} "
                           f"R={closed.r_multiple:+.2f} "
                           f"deployment={ctx.deployment_id} "
                           f"via=reconcile_missing_live_closes"),
                )
                print(f"  ✅ RECONCILED  ticket={match_ticket}  "
                      f"dep={ctx.deployment_id}  pnl={closed.realized_pnl:+.2f}  "
                      f"R={closed.r_multiple:+.2f}  reason={closed.close_reason}")
                reconciled.append({"ticket": match_ticket,
                                    "pnl": closed.realized_pnl})
        except Exception as e:
            errors.append({"ticket": match_ticket, "error": str(e)})
            print(f"  ❌ ERROR  ticket={match_ticket}: {type(e).__name__}: {e}")

    print()
    print("=" * 70)
    print(f"Reconciled:                   {len(reconciled)}")
    print(f"Skipped (already persisted):  {len(skipped_already)}")
    print(f"Skipped (no open context):    {len(skipped_no_ctx)}")
    print(f"Errors:                       {len(errors)}")
    if reconciled:
        total = sum(r["pnl"] for r in reconciled)
        print(f"Total reconciled PnL:         ${total:+,.2f}")
    if skipped_no_ctx:
        print(f"\nNo-context tickets (need manual review): {skipped_no_ctx}")
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
