"""
test_deployment_runner_state.py — pin the restart-survivability +
sizing-pipe + holding-badge logic added in Phase 30b.

DeploymentRunner persists `_last_seen_bar` to runner_state.json next
to the SQLite DB. On restart it MUST reload that map so a Ctrl+C
followed by relaunch doesn't replay bars (which would duplicate-fire
signals against the broker).

Per-deployment risk_pct → lots wiring is verified at the unit level
via core.position_sizer.calc_lots which already has heavy coverage;
here we just confirm the integration code in DeploymentRunner pulls
the right inputs (account_balance, symbol_info).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from core.deployment_runner import DeploymentRunner


# ─────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────

def _make_runner(tmp_path: Path) -> DeploymentRunner:
    """Bare runner — no bridge, no fetcher. We don't need to actually tick."""
    return DeploymentRunner(
        login=999999,
        candle_fetcher=lambda *a, **kw: pd.DataFrame(),
        bridge_call=None,
        db_path=tmp_path / "v2.db",
        poll_seconds=60.0,
    )


def test_save_state_writes_json(tmp_path):
    r = _make_runner(tmp_path)
    r._last_seen_bar["dep1"] = pd.Timestamp("2026-05-04T12:00:00Z")
    r._last_seen_bar["dep2"] = pd.Timestamp("2026-05-04T12:15:00Z")
    r._save_state()
    state_file = r._state_path
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert "last_seen_bar" in data
    assert data["last_seen_bar"]["dep1"].startswith("2026-05-04T12:00:00")
    assert "saved_at_utc" in data


def test_load_state_round_trip(tmp_path):
    """Save → fresh runner → load — last_seen_bar comes back populated."""
    r1 = _make_runner(tmp_path)
    r1._last_seen_bar["dep1"] = pd.Timestamp("2026-05-04T10:00:00Z")
    r1._last_seen_bar["dep2"] = pd.Timestamp("2026-05-04T11:30:00Z")
    r1._save_state()

    # Fresh runner pointed at the same db_path → constructor's _load_state
    # should pick up the JSON automatically
    r2 = _make_runner(tmp_path)
    assert "dep1" in r2._last_seen_bar
    assert "dep2" in r2._last_seen_bar
    assert r2._last_seen_bar["dep1"] == pd.Timestamp("2026-05-04T10:00:00Z")
    assert r2._last_seen_bar["dep2"] == pd.Timestamp("2026-05-04T11:30:00Z")


def test_load_state_missing_file_starts_fresh(tmp_path):
    """No prior state file → empty dict, no exception."""
    r = _make_runner(tmp_path)
    assert r._last_seen_bar == {}
    assert not r._state_path.exists()


def test_load_state_corrupt_file_starts_fresh(tmp_path):
    """Garbage in runner_state.json → don't crash, just start fresh."""
    r1 = _make_runner(tmp_path)
    r1._state_path.parent.mkdir(parents=True, exist_ok=True)
    r1._state_path.write_text("{not valid json")
    # Constructor should swallow the JSONDecodeError and continue
    r2 = _make_runner(tmp_path)
    assert r2._last_seen_bar == {}


def test_save_is_atomic(tmp_path):
    """The save uses a tmp file + replace, so a crash mid-write doesn't
    leave a half-written runner_state.json."""
    r = _make_runner(tmp_path)
    r._last_seen_bar["dep1"] = pd.Timestamp("2026-05-04T12:00:00Z")
    r._save_state()
    # No leftover .tmp file
    tmp_files = list(tmp_path.rglob("runner_state.tmp"))
    assert tmp_files == []
    # The actual file is valid JSON
    json.loads(r._state_path.read_text())


# ─────────────────────────────────────────────────────────────────────
# Account-balance sourcing for sizing
# ─────────────────────────────────────────────────────────────────────

def test_get_account_balance_falls_back_when_bridge_offline(tmp_path,
                                                                  monkeypatch):
    """No bridge_call → falls back to account_manager baseline →
    final fallback to 100k. Never returns 0 (sizing safety)."""
    r = _make_runner(tmp_path)
    # account_manager.get_account returns None for unknown login → 100k fallback
    bal = r._get_account_balance()
    assert bal == 100_000.0


def test_get_account_balance_uses_bridge_equity(tmp_path):
    """When bridge_call is set and account_info returns valid equity,
    use that — that's the real-time live equity, not stale baseline."""
    from dataclasses import dataclass

    @dataclass
    class _FakeAccountInfo:
        login: int = 1
        balance: float = 95_000.0
        equity: float = 96_500.0
        currency: str = "USD"
        leverage: int = 100
        name: str = ""
        server: str = ""
        company: str = ""
        trade_mode: int = 0
        margin: float = 0.0
        margin_free: float = 0.0
        margin_level: float = 0.0

    # Mock the MT5AccountClient class entirely
    import core.deployment_runner as dr
    with patch.object(dr, "_count_open_positions" if False else "log"):
        pass   # noop — we patch via mt5_account below

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        def account_info(self, force_refresh=False):
            return _FakeAccountInfo()
        def positions_get(self):
            return []

    with patch("core.mt5_account.MT5AccountClient", _FakeClient):
        r = DeploymentRunner(
            login=999999,
            candle_fetcher=lambda *a, **kw: pd.DataFrame(),
            bridge_call=lambda *a, **kw: {"ok": True},
            db_path=tmp_path / "v2.db",
        )
        bal = r._get_account_balance()
    # Should pick up the live equity ($96,500), NOT the $100k default
    assert bal == 96_500.0


# ─────────────────────────────────────────────────────────────────────
# Holding badge integration
# ─────────────────────────────────────────────────────────────────────

def test_holding_badge_summarize():
    """summarize() returns the running/holding/flat counts the UI shows."""
    from dataclasses import dataclass
    from dashboards.components.holding_badge import summarize

    @dataclass
    class _Dep:
        deployment_id: str
        strategy: str
        ticker: str
        status: str

    @dataclass
    class _Pos:
        symbol: str
        type: int = 0
        comment: str = ""
        profit: float = 0.0
        price_open: float = 0.0
        ticket: int = 0

    deps = [
        _Dep("d1", "vol_breakout", "US100.cash", "live"),
        _Dep("d2", "ema_cross_9_20", "EURUSD", "live"),
        _Dep("d3", "ibs", "GBPUSD", "live"),
        _Dep("d4", "donchian_20", "USDJPY", "halted"),
    ]
    positions = [
        _Pos(symbol="US100.cash", comment="vol_breakout"),  # matches d1
    ]
    r = summarize(deps, positions)
    assert r["n_running"] == 3      # d1, d2, d3 are live (d4 halted)
    assert r["n_holding"] == 1      # only d1 has a position
    assert r["n_flat"] == 2          # d2, d3 flat
    assert r["n_halted"] == 1


def test_render_badge_holding_vs_flat():
    """Badge HTML shows 💼 when holding, ⚪ when flat."""
    from dataclasses import dataclass
    from dashboards.components.holding_badge import render_badge

    @dataclass
    class _Dep:
        deployment_id: str
        strategy: str
        ticker: str
        status: str

    @dataclass
    class _Pos:
        symbol: str
        type: int = 0
        comment: str = ""
        profit: float = 12.34
        price_open: float = 25234.7
        ticket: int = 99

    dep = _Dep("d1", "vol_breakout", "US100.cash", "live")
    flat_html = render_badge(dep, [])
    assert "⚪" in flat_html and "flat" in flat_html

    holding_html = render_badge(dep, [_Pos(symbol="US100.cash",
                                                 comment="vol_breakout")])
    assert "💼" in holding_html
    assert "LONG" in holding_html
    # Format is "$+12.34" (currency symbol then signed value), NOT "+$12.34".
    assert "$+12.34" in holding_html
    assert "25234" in holding_html
    assert "ticket 99" in holding_html


def test_render_badge_halted_overrides():
    """When deployment.status is halted, badge shows HALTED regardless
    of broker positions."""
    from dataclasses import dataclass
    from dashboards.components.holding_badge import render_badge

    @dataclass
    class _Dep:
        deployment_id: str
        strategy: str
        ticker: str
        status: str

    dep = _Dep("d1", "x", "Y", "halted")
    html = render_badge(dep, [])
    assert "HALTED" in html
