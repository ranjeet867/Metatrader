"""Regression tests for Bug C — live close reconciliation
(`core/live_close_reconciler.py` + DeploymentRunner integration).

Bug C symptom: when the broker auto-closes a bot-opened position via
server-side TP/SL, the position vanished from positions_get() but the
runner never persisted the close to `trades`, never recorded a
`close:{reason}` bridge_event, and the circuit_breaker stayed blind to
realised PnL on live closes.

These tests pin the post-fix behaviour:
  • LiveOpenContext serializes round-trip safely
  • find_close_deal / find_open_deal match by ticket OR position_id
  • compute_r_multiple is correct for LONG and SHORT
  • build_closed_record produces the exact attrs _persist_closed_trade
    requires
  • already_persisted is idempotent — second reconciliation of the
    same ticket is a no-op
  • reconcile_vanished_tickets writes to trades AND records bridge event
  • Edge cases: no vanished tickets, missing open context, unmatched
    OUT deal, multiple OUT deals (partial close picks the latest)

Failure of any test below means we've regressed. Don't weaken these.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from core import storage
from core.live_close_reconciler import (
    BOT_MAGIC,
    LiveOpenContext,
    already_persisted,
    build_closed_record,
    compute_r_multiple,
    find_close_deal,
    find_open_deal,
    infer_close_reason,
    reconcile_vanished_tickets,
)


# ── Fixtures ──────────────────────────────────────────────────────


def _ctx(ticket: int = 148661736,
          deployment_id: str = "rsi_30_70_EURUSD_M15_bidir",
          symbol: str = "EURUSD", tf: str = "M15",
          strategy: str = "rsi_30_70",
          direction: str = "SHORT",
          entry_price: float = 1.17737,
          stop_price: float = 1.17798,
          target_price: float = 1.17676,
          lots: float = 1.92,
          opened_at_utc: str = "2026-05-08T13:00:02+00:00") -> LiveOpenContext:
    return LiveOpenContext(
        ticket=ticket, deployment_id=deployment_id,
        symbol=symbol, tf=tf, strategy=strategy,
        direction=direction,
        entry_price=entry_price, stop_price=stop_price,
        target_price=target_price, lots=lots,
        opened_at_utc=opened_at_utc,
    )


def _deal(ticket: int = 148661736, position_id: int = 148661736,
            entry: int = 1, symbol: str = "EURUSD",
            volume: float = 1.92, price: float = 1.17676,
            profit: float = 117.12, swap: float = 0.0,
            commission: float = -9.60, time_utc: str = "2026-05-08T13:12:17+00:00",
            comment: str = "[tp]") -> SimpleNamespace:
    return SimpleNamespace(
        ticket=ticket, position_id=position_id, entry=entry,
        symbol=symbol, volume=volume, price=price,
        profit=profit, swap=swap, commission=commission,
        time_utc=time_utc, comment=comment, type=1,
    )


@pytest.fixture
def db(tmp_path: Path):
    p = tmp_path / "v2.db"
    storage.init_schema(p)
    return p


# ── LiveOpenContext serialization ──────────────────────────────────


def test_context_round_trip():
    """Context serialized to dict and back must be identical — runner
    state persistence depends on this."""
    c = _ctx()
    d = c.to_dict()
    c2 = LiveOpenContext.from_dict(d)
    assert c.ticket == c2.ticket
    assert c.deployment_id == c2.deployment_id
    assert c.entry_price == c2.entry_price
    assert c.stop_price == c2.stop_price
    assert c.lots == c2.lots
    assert c.direction == c2.direction


# ── Deal matching ──────────────────────────────────────────────────


def test_find_close_deal_matches_by_ticket():
    deals = [
        _deal(ticket=999, entry=0),                     # IN, irrelevant
        _deal(ticket=148661736, entry=1, profit=117),   # OUT, target
        _deal(ticket=888, entry=1, profit=42),          # OUT, different ticket
    ]
    out = find_close_deal(deals, 148661736)
    assert out is not None
    assert int(out.ticket) == 148661736


def test_find_close_deal_matches_by_position_id():
    """Some brokers report close ticket different from open ticket but
    keep position_id consistent. Match by either."""
    deals = [
        _deal(ticket=999, position_id=148661736, entry=1, profit=117),
    ]
    out = find_close_deal(deals, 148661736)
    assert out is not None
    assert int(out.position_id) == 148661736


def test_find_close_deal_picks_latest_on_partial_close():
    """If multiple OUT deals share the position (partial closes), the
    final close is the one that matters — pick latest time_utc."""
    deals = [
        _deal(ticket=148661736, entry=1, profit=50,
              time_utc="2026-05-08T13:05:00+00:00"),
        _deal(ticket=148661736, entry=1, profit=67,
              time_utc="2026-05-08T13:12:17+00:00"),
    ]
    out = find_close_deal(deals, 148661736)
    assert out is not None
    assert out.time_utc == "2026-05-08T13:12:17+00:00"


def test_find_close_deal_returns_none_on_no_match():
    """No deal matches the searched ticket OR position_id."""
    deals = [_deal(ticket=999, position_id=999, entry=1)]
    assert find_close_deal(deals, 148661736) is None


def test_find_close_deal_skips_in_deals():
    """An IN deal (entry==0) must not satisfy a close-deal lookup."""
    deals = [_deal(ticket=148661736, entry=0)]
    assert find_close_deal(deals, 148661736) is None


def test_find_open_deal_matches_in_only():
    deals = [
        _deal(ticket=148661736, entry=0,
              time_utc="2026-05-08T13:00:02+00:00"),
        _deal(ticket=148661736, entry=1,
              time_utc="2026-05-08T13:12:17+00:00"),
    ]
    out = find_open_deal(deals, 148661736)
    assert out is not None
    assert int(out.entry) == 0


# ── R-multiple math ────────────────────────────────────────────────


def test_r_multiple_long_winner():
    """LONG entry 100, stop 95, exit 110 → risk=5, reward=10 → R=2.0"""
    r = compute_r_multiple("LONG", entry=100.0, exit_price=110.0, stop=95.0)
    assert r == pytest.approx(2.0)


def test_r_multiple_long_loser():
    """LONG entry 100, stop 95, exit 95 → R=-1.0 (full stop)"""
    r = compute_r_multiple("LONG", entry=100.0, exit_price=95.0, stop=95.0)
    assert r == pytest.approx(-1.0)


def test_r_multiple_short_winner():
    """SHORT entry 1.17737, stop 1.17798, exit 1.17676 (your real trade)
    risk = 0.00061, reward = 0.00061 → R = +1.0"""
    r = compute_r_multiple(
        "SHORT", entry=1.17737, exit_price=1.17676, stop=1.17798,
    )
    assert r == pytest.approx(1.0, abs=1e-3)


def test_r_multiple_zero_stop_distance_returns_zero():
    """Degenerate case — stop == entry. Must not divide by zero."""
    r = compute_r_multiple("LONG", entry=100.0, exit_price=110.0, stop=100.0)
    assert r == 0.0


# ── Close reason inference ─────────────────────────────────────────


def test_infer_close_reason_tp_tag():
    assert infer_close_reason("[tp]", profit=10.0) == "tp"


def test_infer_close_reason_sl_tag():
    assert infer_close_reason("[sl]", profit=-10.0) == "sl"


def test_infer_close_reason_manual():
    assert infer_close_reason("manual close", profit=5.0) == "manual"


def test_infer_close_reason_falls_back_to_pnl_sign():
    """Empty comment → infer from profit sign."""
    assert infer_close_reason("", profit=10.0) == "tp_or_close"
    assert infer_close_reason("", profit=-10.0) == "sl_or_close"


# ── ClosedRecord builder ───────────────────────────────────────────


def test_build_closed_record_carries_all_required_attrs():
    """The record must have every attr DeploymentRunner._persist_closed_trade
    reads, or persistence will crash on AttributeError."""
    closed = build_closed_record(ctx=_ctx(), out_deal=_deal())
    required = [
        "direction", "entry_price", "stop_price", "target_price",
        "exit_price", "lots", "realized_pnl", "r_multiple",
        "close_reason", "opened_at_utc", "closed_at_utc",
        "idempotency_key",
    ]
    for attr in required:
        assert hasattr(closed, attr), f"missing required attr: {attr}"


def test_build_closed_record_pnl_includes_swap_and_commission():
    """Realized PnL = profit + swap + commission. Must include all three."""
    out = _deal(profit=100.0, swap=-2.5, commission=-7.5)
    closed = build_closed_record(ctx=_ctx(), out_deal=out)
    assert closed.realized_pnl == pytest.approx(90.0)


def test_build_closed_record_idempotency_key_uses_ticket():
    """Idempotency key must be deterministic per ticket so re-running
    reconciliation can't double-insert the same close."""
    closed_a = build_closed_record(ctx=_ctx(ticket=148661736),
                                       out_deal=_deal(ticket=148661736))
    closed_b = build_closed_record(ctx=_ctx(ticket=148661736),
                                       out_deal=_deal(ticket=148661736))
    assert closed_a.idempotency_key == closed_b.idempotency_key
    assert "148661736" in closed_a.idempotency_key


def test_build_closed_record_uses_ctx_stop_for_r_multiple():
    """R-multiple must use the ORIGINAL stop captured at open time, not
    something the broker might have adjusted to. This is why ctx is
    the source of truth for stop/entry/target."""
    ctx = _ctx(direction="LONG", entry_price=100.0, stop_price=95.0)
    out = _deal(price=110.0, profit=10.0)
    closed = build_closed_record(ctx=ctx, out_deal=out)
    assert closed.r_multiple == pytest.approx(2.0)


# ── Idempotency ───────────────────────────────────────────────────


def test_already_persisted_returns_false_on_empty_db(db: Path):
    assert already_persisted(db, 148661736) is False


def test_already_persisted_returns_true_after_insert(db: Path):
    """Insert a row with the close-key, then verify already_persisted
    returns True. Required so re-running backfill is idempotent."""
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute(
            "INSERT INTO runs (run_id, started_at_utc, symbol, tf, "
            "strategy_name, config_json, starting_balance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("dep_x", "2026-05-08T00:00:00+00:00", "EURUSD", "M15",
             "rsi_30_70", "{}", 100000.0),
        )
        c.execute(
            "INSERT INTO trades (run_id, trade_idx, symbol, direction, "
            "opened_at_utc, entry_price, stop_price, lots, mode, "
            "strategy, tf, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("dep_x", 0, "EURUSD", "SHORT",
             "2026-05-08T13:00:02+00:00", 1.17737, 1.17798, 1.92,
             "live", "rsi_30_70", "M15", "close:148661736"),
        )
    assert already_persisted(db, 148661736) is True
    assert already_persisted(db, 999999) is False


# ── reconcile_vanished_tickets — end-to-end ───────────────────────


def test_reconcile_writes_trade_and_bridge_event(db: Path):
    """End-to-end: a vanished ticket gets reconciled — trade row written,
    bridge_event recorded, ticket removed from open_contexts."""
    ctx = _ctx()

    # Mock bridge_call so history_deals_get returns our crafted deal.
    deal = _deal()
    captured_method = []

    def fake_bridge_call(method, params):
        if method == "history_deals_get":
            return {"ok": True, "data": [{
                "ticket": deal.ticket,
                "position_id": deal.position_id,
                "entry": deal.entry,
                "symbol": deal.symbol,
                "volume": deal.volume,
                "price": deal.price,
                "profit": deal.profit,
                "swap": deal.swap,
                "commission": deal.commission,
                "time_utc": deal.time_utc,
                "comment": deal.comment,
                "type": deal.type,
                "order": 0,
            }]}
        return {"ok": True, "data": []}

    persisted = []

    def fake_persist(d, closed):
        # Simulate _persist_closed_trade by writing to trades
        with sqlite3.connect(str(db)) as c:
            c.execute("PRAGMA foreign_keys=OFF")
            c.execute(
                "INSERT OR IGNORE INTO runs (run_id, started_at_utc, "
                "symbol, tf, strategy_name, config_json, starting_balance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (d.deployment_id, "2026-05-08T00:00:00+00:00",
                 d.ticker, d.tf, d.strategy, "{}", 100000.0),
            )
            c.execute(
                "INSERT INTO trades (run_id, trade_idx, symbol, direction, "
                "opened_at_utc, closed_at_utc, entry_price, stop_price, "
                "target_price, exit_price, lots, realized_pnl, r_multiple, "
                "close_reason, mode, strategy, tf, idempotency_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d.deployment_id, 0, d.ticker, closed.direction,
                 closed.opened_at_utc, closed.closed_at_utc,
                 closed.entry_price, closed.stop_price,
                 closed.target_price, closed.exit_price,
                 closed.lots, closed.realized_pnl, closed.r_multiple,
                 closed.close_reason, "live", d.strategy, d.tf,
                 closed.idempotency_key),
            )
        persisted.append((d.deployment_id, closed.realized_pnl,
                            closed.close_reason))

    events_recorded = []

    def fake_record_event(db_path, ts, *, method, ok, latency_ms, error):
        events_recorded.append((method, ok, error))

    deployment = SimpleNamespace(
        deployment_id=ctx.deployment_id, ticker=ctx.symbol, tf=ctx.tf,
        strategy=ctx.strategy, status="live",
    )

    result = reconcile_vanished_tickets(
        open_contexts={ctx.ticket: ctx},
        current_bot_tickets=set(),    # ticket has vanished
        bridge_call=fake_bridge_call,
        db_path=db,
        persist_fn=fake_persist,
        record_event_fn=fake_record_event,
        deployment_lookup_fn=lambda dep_id: deployment if dep_id == ctx.deployment_id else None,
    )

    # Trade row written
    assert len(persisted) == 1
    dep_id, pnl, reason = persisted[0]
    assert dep_id == ctx.deployment_id
    assert pnl == pytest.approx(deal.profit + deal.swap + deal.commission)
    assert reason == "tp"    # comment is "[tp]"
    # Bridge event recorded
    assert len(events_recorded) == 1
    method, ok, err = events_recorded[0]
    assert method.startswith("close:")
    assert ok is True
    assert str(ctx.ticket) in err
    # Result summary
    assert len(result["reconciled"]) == 1
    assert result["reconciled"][0]["ticket"] == ctx.ticket
    assert result["errors"] == []


def test_reconcile_skips_when_no_tickets_vanished(db: Path):
    """If all tracked tickets are still open on the broker, no work is
    done and no bridge call is made."""
    ctx = _ctx()
    bridge_called = []

    def fake_bridge_call(method, params):
        bridge_called.append(method)
        return {"ok": True, "data": []}

    result = reconcile_vanished_tickets(
        open_contexts={ctx.ticket: ctx},
        current_bot_tickets={ctx.ticket},   # still open
        bridge_call=fake_bridge_call,
        db_path=db,
        persist_fn=lambda d, c: None,
        record_event_fn=lambda *args, **kwargs: None,
        deployment_lookup_fn=lambda dep_id: None,
    )

    assert bridge_called == []   # short-circuited
    assert result["reconciled"] == []
    assert result["unmatched"] == []


def test_reconcile_marks_unmatched_when_no_close_deal_found(db: Path):
    """If history_deals_get returns no OUT deal for a vanished ticket,
    the reconciler reports it as unmatched (so the runner retries next
    tick — broker may be lagging) without crashing."""
    ctx = _ctx()

    def fake_bridge_call(method, params):
        return {"ok": True, "data": []}    # empty history

    result = reconcile_vanished_tickets(
        open_contexts={ctx.ticket: ctx},
        current_bot_tickets=set(),
        bridge_call=fake_bridge_call,
        db_path=db,
        persist_fn=lambda d, c: None,
        record_event_fn=lambda *args, **kwargs: None,
        deployment_lookup_fn=lambda dep_id: None,
    )

    assert result["reconciled"] == []
    assert ctx.ticket in result["unmatched"]


def test_reconcile_skips_already_persisted_tickets(db: Path):
    """If a vanished ticket's close-key is already in trades (e.g. from
    a prior reconciliation run), skip — don't double-insert."""
    ctx = _ctx()
    # Pre-seed a trades row with the close-key
    with sqlite3.connect(str(db)) as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute(
            "INSERT INTO runs (run_id, started_at_utc, symbol, tf, "
            "strategy_name, config_json, starting_balance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("dep_x", "2026-05-08T00:00:00+00:00", "EURUSD", "M15",
             "rsi_30_70", "{}", 100000.0),
        )
        c.execute(
            "INSERT INTO trades (run_id, trade_idx, symbol, direction, "
            "opened_at_utc, entry_price, stop_price, lots, mode, "
            "strategy, tf, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("dep_x", 0, "EURUSD", "SHORT",
             "2026-05-08T13:00:02+00:00", 1.17737, 1.17798, 1.92,
             "live", "rsi_30_70", "M15", f"close:{ctx.ticket}"),
        )

    persisted = []
    deal = _deal()

    def fake_bridge_call(method, params):
        # Return a valid deal so history_deals_get succeeds. The
        # reconciler MUST then short-circuit on already_persisted
        # BEFORE attempting to persist again.
        return {"ok": True, "data": [{
            "ticket": deal.ticket, "position_id": deal.position_id,
            "entry": deal.entry, "symbol": deal.symbol,
            "volume": deal.volume, "price": deal.price,
            "profit": deal.profit, "swap": deal.swap,
            "commission": deal.commission, "time_utc": deal.time_utc,
            "comment": deal.comment, "type": deal.type, "order": 0,
        }]}

    result = reconcile_vanished_tickets(
        open_contexts={ctx.ticket: ctx},
        current_bot_tickets=set(),
        bridge_call=fake_bridge_call,
        db_path=db,
        persist_fn=lambda d, c: persisted.append(c),
        record_event_fn=lambda *a, **kw: None,
        deployment_lookup_fn=lambda dep_id: None,
    )

    assert persisted == []   # NO double-write
    assert ctx.ticket in result["skipped_already_persisted"]
    assert result["reconciled"] == []


def test_reconcile_handles_history_fetch_failure(db: Path):
    """If history_deals_get raises, all vanished tickets are reported as
    errors but the reconciler doesn't crash. Caller can retry next tick."""
    ctx = _ctx()

    def fake_bridge_call(method, params):
        raise RuntimeError("bridge timeout")

    result = reconcile_vanished_tickets(
        open_contexts={ctx.ticket: ctx},
        current_bot_tickets=set(),
        bridge_call=fake_bridge_call,
        db_path=db,
        persist_fn=lambda d, c: None,
        record_event_fn=lambda *a, **kw: None,
        deployment_lookup_fn=lambda dep_id: None,
    )

    assert result["reconciled"] == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["ticket"] == ctx.ticket


def test_reconcile_creates_stub_deployment_if_lookup_fails(db: Path):
    """If the deployment was removed but a ticket from it is still
    being reconciled, build a stub so the trade row still gets written
    (we'd rather have orphan-attributed data than lose the close)."""
    ctx = _ctx()
    deal = _deal()

    def fake_bridge_call(method, params):
        return {"ok": True, "data": [{
            "ticket": deal.ticket, "position_id": deal.position_id,
            "entry": deal.entry, "symbol": deal.symbol,
            "volume": deal.volume, "price": deal.price,
            "profit": deal.profit, "swap": deal.swap,
            "commission": deal.commission, "time_utc": deal.time_utc,
            "comment": deal.comment, "type": deal.type, "order": 0,
        }]}

    persisted = []

    def fake_persist(d, closed):
        persisted.append((d, closed))

    result = reconcile_vanished_tickets(
        open_contexts={ctx.ticket: ctx},
        current_bot_tickets=set(),
        bridge_call=fake_bridge_call,
        db_path=db,
        persist_fn=fake_persist,
        record_event_fn=lambda *a, **kw: None,
        deployment_lookup_fn=lambda dep_id: None,    # always fails
    )

    # Persist still called, with a stub deployment
    assert len(persisted) == 1
    d, closed = persisted[0]
    assert d.deployment_id == ctx.deployment_id
    assert d.ticker == ctx.symbol
    assert closed.realized_pnl != 0
