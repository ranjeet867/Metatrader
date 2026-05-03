"""
mt5_status.py — bridge connection dot + account info card.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from core.mt5_account import MT5AccountClient


def render_bridge_dot(container=None, *, last_latency_ms: int | None = None) -> None:
    """Tiny coloured dot indicating bridge state. Called from sidebar."""
    target = container or st
    if last_latency_ms is None:
        target.markdown("🔴  Bridge: **unknown** (no recent ping)")
    elif last_latency_ms < 100:
        target.markdown(f"🟢  Bridge: **OK** ({last_latency_ms}ms)")
    elif last_latency_ms < 1000:
        target.markdown(f"🟡  Bridge: **slow** ({last_latency_ms}ms)")
    else:
        target.markdown(f"🔴  Bridge: **down** (>{last_latency_ms}ms)")


def render_account_card(client: MT5AccountClient | None,
                         container=None) -> None:
    """Account balance/equity/margin card."""
    target = container or st
    if client is None:
        target.caption("(MT5 client not configured)")
        return
    try:
        ai = client.account_info()
        target.markdown(
            f"**Account #{ai.login}**  "
            f"&nbsp;&nbsp; balance ${ai.balance:,.2f}  "
            f"&nbsp;&nbsp; equity ${ai.equity:,.2f}  "
            f"&nbsp;&nbsp; {ai.currency} • {ai.leverage}x"
        )
    except Exception as e:
        target.caption(f"(account query failed: {e})")


def render_emergency_stop_indicator(stop_dir: Path,
                                      container=None) -> bool:
    """Return True if EMERGENCY_STOP file exists. Renders a banner if so."""
    target = container or st
    sentinel = stop_dir / "EMERGENCY_STOP"
    if sentinel.exists():
        target.error(
            "⛔  **EMERGENCY STOP active** — live orders refused.  "
            f"Remove `{sentinel}` to lift."
        )
        return True
    return False
