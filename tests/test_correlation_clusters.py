"""tests/test_correlation_clusters.py — cluster-cap correctness."""
from __future__ import annotations

from core.correlation_clusters import (
    check_cluster_open, cluster_status, default_clusters, find_cluster,
)
from core.position_guard import OpenPosition


def _pos(symbol: str, dep_id: str = "d1", side: str = "LONG"):
    return OpenPosition(
        deployment_id=dep_id, symbol=symbol, side=side,
        lots=0.10, opened_at_utc="2026-05-06T12:00:00Z",
    )


def test_default_clusters_has_known_groupings():
    c = default_clusters()
    assert "metals_long" in c
    assert "XAUUSD" in c["metals_long"]
    assert "XAGUSD" in c["metals_long"]
    assert "XPTUSD" in c["metals_long"]
    assert "XPDUSD" in c["metals_long"]
    assert "us_indices" in c
    assert "US100.cash" in c["us_indices"]


def test_find_cluster_returns_none_for_unknown_symbol():
    assert find_cluster("SOMETHING.exotic", default_clusters()) is None


def test_find_cluster_returns_cluster_name_for_known_symbol():
    assert find_cluster("XAGUSD", default_clusters()) == "metals_long"
    assert find_cluster("US500.cash", default_clusters()) == "us_indices"


def test_unknown_symbol_always_allowed():
    res = check_cluster_open(
        symbol="UNKNOWN.thing",
        open_positions=[_pos("XAUUSD"), _pos("XAGUSD")],
    )
    assert res.decision == "ALLOW"
    assert res.cluster_name is None


def test_cluster_under_cap_allows_open():
    # 1 metal open, cap=2 → 2nd metal should open
    res = check_cluster_open(
        symbol="XAGUSD",
        open_positions=[_pos("XAUUSD", dep_id="d1")],
        max_concurrent_per_cluster=2,
    )
    assert res.decision == "ALLOW"
    assert res.cluster_name == "metals_long"
    assert res.current_count == 1
    assert res.cap == 2


def test_cluster_at_cap_blocks_open():
    # 2 metals open, cap=2 → 3rd metal blocks
    res = check_cluster_open(
        symbol="XPTUSD",
        open_positions=[_pos("XAUUSD"), _pos("XAGUSD")],
        max_concurrent_per_cluster=2,
    )
    assert res.decision == "BLOCK"
    assert res.cluster_name == "metals_long"
    assert res.current_count == 2


def test_cluster_at_cap_does_not_block_other_cluster():
    # 2 metals open, but US500 in us_indices cluster — should allow
    res = check_cluster_open(
        symbol="US500.cash",
        open_positions=[_pos("XAUUSD"), _pos("XAGUSD")],
        max_concurrent_per_cluster=2,
    )
    assert res.decision == "ALLOW"
    assert res.cluster_name == "us_indices"
    assert res.current_count == 0


def test_cluster_status_snapshots_utilization():
    snap = cluster_status(
        open_positions=[_pos("XAUUSD"), _pos("US500.cash")],
        cap=2,
    )
    assert snap["metals_long"]["open"] == 1
    assert snap["metals_long"]["at_cap"] is False
    assert snap["us_indices"]["open"] == 1
    assert snap["europe_indices"]["open"] == 0
    assert "XAUUSD" in snap["metals_long"]["symbols"]


def test_cap_of_one_is_strict():
    # cap=1 means cluster acts like the silver cluster: at most 1 open
    res = check_cluster_open(
        symbol="XAGUSD",
        open_positions=[_pos("XAUUSD")],
        max_concurrent_per_cluster=1,
    )
    assert res.decision == "BLOCK"
    assert res.current_count == 1
