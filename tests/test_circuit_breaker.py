"""
test_circuit_breaker.py — pin the system-wide stop-loss/target halt logic.

State transitions:
  OK       — no thresholds breached
  STOP_NEW — daily target / total target / max open positions hit. Existing
             trades ride to SL/TP, but no new entries open.
  HALT     — daily loss / total loss / max-consec-losses breached. Refuse
             everything. Auto-pause live deployments if cfg.halt_on_breach.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import circuit_breaker as cb
from core import storage


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "v2.db"
    storage.init_schema(db_path)
    return db_path


def _insert_trade(db_path: Path, *, mode: str, realized_pnl: float,
                   closed_utc: str, run_id: str = "r1", trade_idx: int = 0):
    with sqlite3.connect(str(db_path)) as c:
        c.execute(
            """INSERT INTO trades
               (run_id, trade_idx, symbol, direction,
                opened_at_utc, closed_at_utc,
                entry_price, stop_price, target_price, exit_price,
                lots, realized_pnl, r_multiple, close_reason,
                mode, strategy, tf)
               VALUES (?, ?, 'X', 'LONG',
                       ?, ?,
                       100.0, 99.0, 102.0, 101.0,
                       1.0, ?, ?, 'target',
                       ?, 'test_strat', 'D1')""",
            (run_id, trade_idx, closed_utc, closed_utc,
             realized_pnl, realized_pnl, mode),
        )


def test_default_config_thresholds():
    cfg = cb.CircuitConfig.default()
    assert cfg.daily_loss_dollars == 4_500.0
    assert cfg.total_loss_dollars == 9_000.0
    assert cfg.max_consec_losses == 5


def test_ok_when_no_trades(tmp_db):
    s = cb.evaluate(tmp_db, mode="live")
    assert s.state == "OK"
    assert s.realised_today == 0.0
    assert s.realised_total == 0.0
    assert s.consec_losses == 0


def test_halt_on_total_loss(tmp_db):
    cfg = cb.CircuitConfig(total_loss_dollars=1_000.0,
                              daily_loss_dollars=None,
                              max_consec_losses=None)
    _insert_trade(tmp_db, mode="live", realized_pnl=-1_500.0,
                   closed_utc="2026-05-03T12:00:00+00:00")
    s = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    assert s.state == "HALT"
    assert any("TOTAL LOSS" in r for r in s.reasons)


def test_halt_on_daily_loss_today_only(tmp_db):
    """Yesterday's loss doesn't trigger today's daily-loss rule."""
    cfg = cb.CircuitConfig(daily_loss_dollars=500.0,
                              total_loss_dollars=None,
                              max_consec_losses=None)
    # Big loss yesterday
    _insert_trade(tmp_db, mode="live", realized_pnl=-1_000.0,
                   closed_utc="2026-05-02T12:00:00+00:00", trade_idx=0)
    s_yesterday = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    # Daily would fire ONLY for today's calendar UTC date — yesterday's
    # loss doesn't count. Streak still 1 but no daily-loss reason.
    daily_reasons = [r for r in s_yesterday.reasons if "DAILY LOSS" in r]
    assert daily_reasons == []


def test_halt_on_consec_losses(tmp_db):
    cfg = cb.CircuitConfig(max_consec_losses=3,
                              daily_loss_dollars=None,
                              total_loss_dollars=None)
    for i in range(3):
        _insert_trade(tmp_db, mode="live", realized_pnl=-100.0,
                       closed_utc=f"2026-05-03T{i:02d}:00:00+00:00",
                       trade_idx=i)
    s = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    assert s.state == "HALT"
    assert s.consec_losses == 3
    assert any("CONSEC LOSSES" in r for r in s.reasons)


def test_winning_trade_breaks_streak(tmp_db):
    cfg = cb.CircuitConfig(max_consec_losses=3,
                              daily_loss_dollars=None,
                              total_loss_dollars=None)
    # 2 losses, 1 win, 1 loss → current streak = 1 (last trade only)
    _insert_trade(tmp_db, mode="live", realized_pnl=-100.0,
                   closed_utc="2026-05-03T01:00:00+00:00", trade_idx=0)
    _insert_trade(tmp_db, mode="live", realized_pnl=-100.0,
                   closed_utc="2026-05-03T02:00:00+00:00", trade_idx=1)
    _insert_trade(tmp_db, mode="live", realized_pnl=+200.0,
                   closed_utc="2026-05-03T03:00:00+00:00", trade_idx=2)
    _insert_trade(tmp_db, mode="live", realized_pnl=-100.0,
                   closed_utc="2026-05-03T04:00:00+00:00", trade_idx=3)
    s = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    assert s.consec_losses == 1
    assert s.state == "OK"


def test_stop_new_on_daily_target(tmp_db):
    cfg = cb.CircuitConfig(daily_target_dollars=500.0,
                              daily_loss_dollars=None,
                              total_loss_dollars=None,
                              max_consec_losses=None)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _insert_trade(tmp_db, mode="live", realized_pnl=+750.0,
                   closed_utc=f"{today}T12:00:00+00:00")
    s = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    assert s.state == "STOP_NEW"
    assert any("DAILY TARGET" in r for r in s.reasons)


def test_stop_new_on_max_open_positions(tmp_db):
    cfg = cb.CircuitConfig(max_open_positions=3,
                              daily_loss_dollars=None,
                              total_loss_dollars=None,
                              max_consec_losses=None)
    s = cb.evaluate(tmp_db, mode="live", cfg=cfg, open_positions_count=3)
    assert s.state == "STOP_NEW"
    assert any("MAX OPEN POSITIONS" in r for r in s.reasons)


def test_halt_overrides_stop_new(tmp_db):
    """If both a STOP_NEW and a HALT condition fire, HALT wins."""
    cfg = cb.CircuitConfig(daily_loss_dollars=500.0,
                              daily_target_dollars=300.0,
                              total_loss_dollars=None,
                              max_consec_losses=None,
                              max_open_positions=None)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _insert_trade(tmp_db, mode="live", realized_pnl=-1_000.0,
                   closed_utc=f"{today}T12:00:00+00:00")
    s = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    assert s.state == "HALT"


def test_paper_and_live_isolated(tmp_db):
    """Live circuit doesn't fire on paper losses."""
    cfg = cb.CircuitConfig(total_loss_dollars=500.0,
                              daily_loss_dollars=None,
                              max_consec_losses=None)
    _insert_trade(tmp_db, mode="paper", realized_pnl=-1_000.0,
                   closed_utc="2026-05-03T12:00:00+00:00")
    s_live = cb.evaluate(tmp_db, mode="live", cfg=cfg)
    s_paper = cb.evaluate(tmp_db, mode="paper", cfg=cfg)
    assert s_live.state == "OK"
    assert s_paper.state == "HALT"


def test_status_block_new_property():
    s = cb.CircuitStatus(state="OK")
    assert not s.block_new
    s = cb.CircuitStatus(state="STOP_NEW")
    assert s.block_new
    s = cb.CircuitStatus(state="HALT")
    assert s.block_new
    assert not s.is_ok


def test_save_load_roundtrip(tmp_path, monkeypatch):
    """Per-account config persists to JSON."""
    from core import account_manager
    monkeypatch.setattr(account_manager, "get_deployments_path",
                          lambda login: tmp_path / f"{login}/deployments.json")
    cfg_in = cb.CircuitConfig(daily_loss_dollars=2_000.0,
                                 max_consec_losses=4,
                                 halt_on_breach=False)
    cb.save_config(login=12345, cfg=cfg_in)
    cfg_out = cb.load_config(login=12345)
    assert cfg_out == cfg_in


def test_load_config_missing_returns_default(tmp_path, monkeypatch):
    from core import account_manager
    monkeypatch.setattr(account_manager, "get_deployments_path",
                          lambda login: tmp_path / f"{login}/deployments.json")
    cfg = cb.load_config(login=99999)
    assert cfg == cb.CircuitConfig.default()
