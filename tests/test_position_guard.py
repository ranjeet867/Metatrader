"""
test_position_guard.py — pin the cross-deployment collision logic.

Three policies × the cases (no collision, same side, opposite side,
same deployment retry).
"""
from __future__ import annotations

import pytest

from core.position_guard import (
    GuardResult, OpenPosition, check_open, collisions_in_open_set,
)


def _pos(dep_id, symbol, side):
    return OpenPosition(deployment_id=dep_id, symbol=symbol, side=side,
                          lots=1.0, opened_at_utc="2026-05-03T12:00")


def test_allow_when_no_open_positions():
    r = check_open(new_symbol="EURUSD", new_side="LONG",
                    new_deployment_id="dep1", open_positions=[],
                    policy="strict")
    assert r.decision == "ALLOW"


def test_allow_when_other_symbol_open():
    r = check_open(new_symbol="EURUSD", new_side="LONG",
                    new_deployment_id="dep1",
                    open_positions=[_pos("dep2", "GBPUSD", "LONG")],
                    policy="strict")
    assert r.decision == "ALLOW"


def test_allow_when_same_deployment_already_open():
    """A deployment's own existing position doesn't block its own retry —
    the executor handles that case (it just won't open a duplicate)."""
    r = check_open(new_symbol="EURUSD", new_side="LONG",
                    new_deployment_id="dep1",
                    open_positions=[_pos("dep1", "EURUSD", "LONG")],
                    policy="strict")
    assert r.decision == "ALLOW"


def test_strict_blocks_any_other_deployment_same_symbol():
    r = check_open(new_symbol="EURUSD", new_side="LONG",
                    new_deployment_id="dep1",
                    open_positions=[_pos("dep2", "EURUSD", "LONG")],
                    policy="strict")
    assert r.decision == "BLOCK"
    assert r.conflicting_deployment_id == "dep2"
    assert "dep2" in r.reason


def test_strict_blocks_opposite_side_too():
    r = check_open(new_symbol="EURUSD", new_side="SHORT",
                    new_deployment_id="dep1",
                    open_positions=[_pos("dep2", "EURUSD", "LONG")],
                    policy="strict")
    assert r.decision == "BLOCK"


def test_side_match_blocks_opposite_side():
    r = check_open(new_symbol="EURUSD", new_side="SHORT",
                    new_deployment_id="dep1",
                    open_positions=[_pos("dep2", "EURUSD", "LONG")],
                    policy="side_match")
    assert r.decision == "BLOCK"


def test_side_match_stacks_same_side():
    r = check_open(new_symbol="EURUSD", new_side="LONG",
                    new_deployment_id="dep1",
                    open_positions=[_pos("dep2", "EURUSD", "LONG")],
                    policy="side_match")
    assert r.decision == "STACK"
    assert "dep2" in r.reason


def test_hedging_allows_everything():
    r = check_open(new_symbol="EURUSD", new_side="SHORT",
                    new_deployment_id="dep1",
                    open_positions=[
                        _pos("dep2", "EURUSD", "LONG"),
                        _pos("dep3", "EURUSD", "LONG"),
                    ],
                    policy="hedging")
    assert r.decision == "ALLOW"


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        check_open(new_symbol="EURUSD", new_side="WRONG",
                    new_deployment_id="dep1", open_positions=[],
                    policy="strict")


def test_collisions_in_open_set_finds_pairs():
    positions = [
        _pos("dep1", "EURUSD", "LONG"),
        _pos("dep2", "EURUSD", "SHORT"),    # collision with dep1
        _pos("dep3", "GBPUSD", "LONG"),
        _pos("dep4", "EURUSD", "LONG"),    # also collides with dep1, dep2
    ]
    pairs = collisions_in_open_set(positions)
    # Each pair counted once: (dep1,dep2), (dep1,dep4), (dep2,dep4)
    assert len(pairs) == 3
    pair_keys = {(a.deployment_id, b.deployment_id) for a, b in pairs}
    assert ("dep1", "dep2") in pair_keys


def test_collisions_in_open_set_empty():
    assert collisions_in_open_set([]) == []
    assert collisions_in_open_set([_pos("d", "EURUSD", "LONG")]) == []
