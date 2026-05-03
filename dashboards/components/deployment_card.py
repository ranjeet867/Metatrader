"""
deployment_card.py — one card per (strategy × ticker × tf × account).

Shows status badge, today/week/month PnL, open positions, last signal,
next forced flat. Action buttons: Backtest / Paper / Go Live / Pause / Remove.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core import account_manager, deployment as dep_mod
from core.deployment import Deployment


_BADGE_BY_STATUS = {
    "idle":   "💤 IDLE",
    "paper":  "🟡 PAPER",
    "live":   "🟢 LIVE",
    "paused": "⏸ PAUSED",
    "halted": "⛔ HALTED",
}


def _badge_html(status: str) -> str:
    text = _BADGE_BY_STATUS.get(status, status.upper())
    color = {
        "idle":   "#666",
        "paper":  "#b08800",
        "live":   "#0c8a3a",
        "paused": "#aa6a00",
        "halted": "#aa1a1a",
    }.get(status, "#666")
    return (f"<span style='background:{color};color:white;padding:3px 8px;"
            f"border-radius:4px;font-size:0.85rem;'>{text}</span>")


def render(*, login: int, dep: Deployment,
            on_go_live=None, on_paper=None, on_pause=None, on_remove=None,
            on_backtest=None, container=None):
    """Render a single deployment card. Action callbacks are optional —
    when provided, the buttons trigger them."""
    target = container or st
    status = "halted" if account_manager.emergency_stop_active() and dep.status == "live" else dep.status
    with target.container(border=True):
        title_cols = st.columns([6, 2])
        title_cols[0].markdown(
            f"### {dep.strategy} × `{dep.ticker}` × `{dep.tf}` "
            + ("(long-only)" if dep.long_only else "(bidir)")
        )
        title_cols[1].markdown(_badge_html(status), unsafe_allow_html=True)

        st.caption(
            f"Risk **{dep.risk_pct:.2f}%/trade**  ·  "
            f"Daily cap **{dep.daily_cap_pct:.2f}%**"
        )

        # Last started / last signal — shown if known
        meta_cols = st.columns(3)
        if dep.last_started_at_utc:
            meta_cols[0].caption(f"Started: `{dep.last_started_at_utc[:16]}`")
        else:
            meta_cols[0].caption("Started: —")
        if dep.last_signal_at_utc:
            meta_cols[1].caption(f"Last signal: `{dep.last_signal_at_utc[:16]}`")
        else:
            meta_cols[1].caption("Last signal: —")
        meta_cols[2].caption(f"Account: `{login}`")

        # Action buttons
        act = st.columns(5)
        bt_clicked = act[0].button("📊 Backtest", key=f"bt_{login}_{dep.deployment_id}",
                                       use_container_width=True)
        paper_clicked = act[1].button(
            "📡 Paper", key=f"pp_{login}_{dep.deployment_id}",
            use_container_width=True,
            disabled=(dep.status == "live"),
        )
        live_clicked = act[2].button(
            "🚀 Go Live",
            key=f"gl_{login}_{dep.deployment_id}",
            type="primary",
            use_container_width=True,
            disabled=account_manager.emergency_stop_active() or dep.status == "live",
        )
        pause_label = "▶  Resume" if dep.status == "paused" else "⏸ Pause"
        pause_clicked = act[3].button(
            pause_label, key=f"pa_{login}_{dep.deployment_id}",
            use_container_width=True,
            disabled=(dep.status == "idle"),
        )
        remove_clicked = act[4].button(
            "🗑 Remove", key=f"rm_{login}_{dep.deployment_id}",
            use_container_width=True,
        )
        if bt_clicked and on_backtest:
            on_backtest(dep)
        if paper_clicked and on_paper:
            on_paper(dep)
        if live_clicked and on_go_live:
            on_go_live(dep)
        if pause_clicked and on_pause:
            on_pause(dep)
        if remove_clicked and on_remove:
            on_remove(dep)
