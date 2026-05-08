"""
holding_badge.py — "is this deployment holding an open position right now?"

The deployment.status field tells you whether a deployment is ALLOWED to
trade (live / paper / halted / paused). It does NOT tell you whether the
deployment currently has an open position on the broker.

This helper bridges the gap by matching:
  deployment(strategy, ticker, tf) ←→ broker_position(symbol, comment)

When the runner places an order via LiveExecutor.send_order, it stamps
the comment with the strategy name (e.g. "vol_breakout"). We use that +
the symbol match to identify which deployment owns each open position.

Usage in any deployment card:
    badge_html = render_badge(deployment, broker_positions)
    st.markdown(badge_html, unsafe_allow_html=True)
"""
from __future__ import annotations

from typing import Iterable, Optional


def find_position_for_deployment(deployment, broker_positions: Iterable):
    """Return the broker position currently held by this deployment, or
    None if it's flat. Matching rules (in order):

      1. Symbol == deployment.ticker  AND  comment contains strategy name
      2. Symbol == deployment.ticker  AND  no other deployment claims it
      3. None — deployment is flat
    """
    if not broker_positions:
        return None
    same_symbol = [p for p in broker_positions
                    if getattr(p, "symbol", "") == deployment.ticker]
    if not same_symbol:
        return None
    # Best match: comment includes strategy name (or its base)
    strat_lower = (deployment.strategy or "").lower()
    base = strat_lower.rsplit("_", 2)[0] if "_" in strat_lower else strat_lower
    for p in same_symbol:
        cmt = (getattr(p, "comment", "") or "").lower()
        if (strat_lower and strat_lower in cmt) or (base and base in cmt):
            return p
    # Fallback: if only one position on this symbol, claim it
    if len(same_symbol) == 1:
        return same_symbol[0]
    return None


def render_badge(deployment, broker_positions: Iterable) -> str:
    """Return an HTML badge string for the deployment's current state.

      💼 LONG @ 25234.7 · +$45  (holding)
      ⚪ flat — waiting for signal
      ⛔ paused / halted (status overrides)
    """
    if deployment.status in ("paused", "halted", "idle"):
        color = ("#dc2626" if deployment.status == "halted"
                 else "#9ca3af")
        label = deployment.status.upper()
        return (f"<span style='color:{color};font-size:0.78rem;"
                f"font-weight:600;'>⛔ {label}</span>")

    pos = find_position_for_deployment(deployment, broker_positions)
    if pos is None:
        return (f"<span style='color:#9ca3af;font-size:0.78rem;"
                f"font-weight:500;'>⚪ flat — waiting for signal</span>")

    side = "LONG" if int(getattr(pos, "type", 0)) == 0 else "SHORT"
    entry = float(getattr(pos, "price_open", 0.0))
    pnl = float(getattr(pos, "profit", 0.0) or 0.0)
    pnl_color = "#16a34a" if pnl >= 0 else "#dc2626"
    side_color = "#16a34a" if side == "LONG" else "#dc2626"
    return (
        f"<span style='font-size:0.78rem;font-weight:600;'>"
        f"💼 <span style='color:{side_color};'>{side}</span> "
        f"@ {entry:.4f} · "
        f"<span style='color:{pnl_color};'>${pnl:+.2f}</span> "
        f"<span style='color:#9ca3af;font-weight:400;'>"
        f"(ticket {int(getattr(pos, 'ticket', 0))})</span></span>"
    )


def summarize(deployments: Iterable, broker_positions: Iterable) -> dict:
    """Return {n_running, n_holding, n_flat, n_halted} for the header
    pill on the Live / Operations page."""
    deployments = list(deployments)
    broker_positions = list(broker_positions or [])
    n_running = sum(1 for d in deployments
                       if d.status in ("paper", "live"))
    n_halted = sum(1 for d in deployments if d.status == "halted")
    n_holding = 0
    for d in deployments:
        if d.status not in ("paper", "live"):
            continue
        if find_position_for_deployment(d, broker_positions) is not None:
            n_holding += 1
    return {
        "n_running": n_running,
        "n_holding": n_holding,
        "n_flat": n_running - n_holding,
        "n_halted": n_halted,
    }
