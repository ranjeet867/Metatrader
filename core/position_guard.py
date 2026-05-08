"""
position_guard.py — block colliding positions across deployments.

Two deployments on the same symbol firing on the same bar = a real
problem on a real broker. Even when both win, you've doubled exposure
without meaning to; when they conflict (one LONG, one SHORT) you're
either hedging (some brokers / FTMO accounts disallow this) or netting
(positions cancel) — both undesirable surprises.

This module sits BETWEEN the strategy signal and the order-open call.
PaperExecutor and LiveExecutor both ask: `should_open(symbol, side)?`
and the guard returns ALLOW / BLOCK / STACK with a reason.

Policies:
  • strict   — only one position per symbol, regardless of side or
               which deployment opened it. Recommended for FTMO.
  • side_match — one position per symbol, but a same-side signal from
               another deployment is silently dropped (rather than
               opening a duplicate). Different side = always BLOCK.
  • hedging  — anything goes. For brokers that allow hedging accounts
               and users who know what they're doing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal


GuardDecision = Literal["ALLOW", "BLOCK", "STACK"]
GuardPolicy = Literal["strict", "side_match", "hedging"]


@dataclass(frozen=True)
class OpenPosition:
    """Snapshot of one open position from any deployment on the
    account. Used to decide whether a new signal can fire."""
    deployment_id: str       # which deployment owns it
    symbol: str
    side: str                # "LONG" | "SHORT"
    lots: float
    opened_at_utc: str

    @property
    def is_long(self) -> bool:
        return self.side.upper() == "LONG"


@dataclass(frozen=True)
class GuardResult:
    decision: GuardDecision
    reason: str
    conflicting_deployment_id: str | None = None


def check_open(
    *,
    new_symbol: str,
    new_side: str,
    new_deployment_id: str,
    open_positions: Iterable[OpenPosition],
    policy: GuardPolicy = "strict",
) -> GuardResult:
    """Decide whether a new open is allowed given the current positions.

    `open_positions` should include positions from EVERY deployment on
    the account — paper and live alike. The decision is independent of
    mode: a paper LONG on EURUSD blocks a live SHORT on EURUSD because
    the real-world implication (opposite signals on the same symbol)
    is the same risk regardless of whether the paper one is virtual.
    """
    new_side_u = new_side.upper()
    if new_side_u not in ("LONG", "SHORT"):
        raise ValueError(f"side must be LONG or SHORT, got {new_side!r}")

    # Find all positions on the same symbol owned by OTHER deployments
    same_symbol = [p for p in open_positions if p.symbol == new_symbol
                    and p.deployment_id != new_deployment_id]

    if not same_symbol:
        return GuardResult(decision="ALLOW",
                              reason="no other position on this symbol")

    if policy == "hedging":
        return GuardResult(
            decision="ALLOW",
            reason=(f"hedging policy permits a {new_side_u} alongside "
                     f"{len(same_symbol)} existing position(s)"),
        )

    # In strict mode, ANY existing position on this symbol blocks us
    if policy == "strict":
        first = same_symbol[0]
        return GuardResult(
            decision="BLOCK",
            reason=(f"strict policy: {new_side_u} on {new_symbol} blocked — "
                     f"deployment `{first.deployment_id}` already has a "
                     f"{first.side} position open"),
            conflicting_deployment_id=first.deployment_id,
        )

    # side_match: opposite side blocks; same side stacks (i.e. silently
    # drops the new signal because a same-side position is effectively
    # the same trade)
    opposite = [p for p in same_symbol
                 if p.side.upper() != new_side_u]
    if opposite:
        first = opposite[0]
        return GuardResult(
            decision="BLOCK",
            reason=(f"side_match policy: {new_side_u} on {new_symbol} "
                     f"conflicts with `{first.deployment_id}`'s {first.side}"),
            conflicting_deployment_id=first.deployment_id,
        )
    same_side = same_symbol[0]
    return GuardResult(
        decision="STACK",
        reason=(f"side_match policy: {new_side_u} on {new_symbol} dropped — "
                 f"`{same_side.deployment_id}` already has same-side "
                 f"{same_side.side} position"),
        conflicting_deployment_id=same_side.deployment_id,
    )


def collisions_in_open_set(
    open_positions: Iterable[OpenPosition],
) -> list[tuple[OpenPosition, OpenPosition]]:
    """Diagnostic: find pairs of currently-open positions that violate
    the strict policy (same symbol, different deployments). Useful for
    a "current state" dashboard panel — even if guards block new opens,
    pre-existing collisions must be surfaced so the operator can fix
    them."""
    positions = list(open_positions)
    out = []
    for i, a in enumerate(positions):
        for b in positions[i + 1:]:
            if a.symbol == b.symbol and a.deployment_id != b.deployment_id:
                out.append((a, b))
    return out
