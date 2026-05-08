"""tests/test_runner_phantom_filter.py — verify _open_positions_snapshot
filters phantom (non-bot-magic) broker positions out of the guard snapshot
so they don't block legitimate bot opens.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from core.deployment import Deployment
from core.deployment_runner import DeploymentRunner


@dataclass
class _FakeBridgePos:
    """Stand-in for core.mt5_account.BridgePosition."""
    ticket: int = 1
    symbol: str = ""
    type: int = 0          # 0=LONG, 1=SHORT
    volume: float = 0.10
    magic: int = 0
    time_setup: str = "2026-05-07T14:00:00Z"


def _make_runner(tmp_path):
    return DeploymentRunner(
        login=12345, bridge_call=MagicMock(),
        candle_fetcher=MagicMock(),
        db_path=tmp_path / "v2.db",
    )


def test_phantom_positions_filtered_out(tmp_path):
    runner = _make_runner(tmp_path)
    # Mock the bridge: returns 2 positions — 1 bot, 1 phantom
    bridge_positions = [
        _FakeBridgePos(symbol="XAUUSD", magic=0, volume=0.01),       # phantom
        _FakeBridgePos(symbol="EURUSD", magic=999_001, volume=0.10), # bot
    ]
    deployments = [
        Deployment(
            deployment_id="rsi_30_70_EURUSD_M15_long",
            strategy="rsi_30_70", ticker="EURUSD", tf="M15",
            status="live",
        ),
    ]
    with patch("core.mt5_account.MT5AccountClient") as mock_cls:
        mock_cls.return_value.positions_get.return_value = bridge_positions
        snapshot = runner._open_positions_snapshot(deployments)
    # Should only contain the bot position, not the phantom
    syms = [p.symbol for p in snapshot]
    assert "EURUSD" in syms
    assert "XAUUSD" not in syms, (
        "phantom XAUUSD position must be filtered out so it doesn't "
        "consume position_guard / correlation_clusters slots"
    )


def test_only_bot_magic_positions_kept(tmp_path):
    runner = _make_runner(tmp_path)
    bridge_positions = [
        _FakeBridgePos(symbol="XAUUSD", magic=0),         # phantom
        _FakeBridgePos(symbol="XAGUSD", magic=12345),     # someone else's EA
        _FakeBridgePos(symbol="EURUSD", magic=999_001),   # bot
        _FakeBridgePos(symbol="JP225.cash", magic=999_001),  # bot
    ]
    deployments = [
        Deployment(deployment_id="d1", strategy="x", ticker="EURUSD",
                    tf="M15", status="live"),
        Deployment(deployment_id="d2", strategy="x", ticker="JP225.cash",
                    tf="M15", status="live"),
    ]
    with patch("core.mt5_account.MT5AccountClient") as mock_cls:
        mock_cls.return_value.positions_get.return_value = bridge_positions
        snapshot = runner._open_positions_snapshot(deployments)
    syms = sorted(p.symbol for p in snapshot)
    assert syms == ["EURUSD", "JP225.cash"]


def test_all_phantoms_returns_empty_snapshot(tmp_path):
    runner = _make_runner(tmp_path)
    bridge_positions = [
        _FakeBridgePos(symbol="XAUUSD", magic=0),
        _FakeBridgePos(symbol="HK50.cash", magic=0),
    ]
    with patch("core.mt5_account.MT5AccountClient") as mock_cls:
        mock_cls.return_value.positions_get.return_value = bridge_positions
        snapshot = runner._open_positions_snapshot([])
    assert snapshot == []
