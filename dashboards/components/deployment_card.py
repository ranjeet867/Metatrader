"""
deployment_card.py — one card per (strategy × ticker × tf × account).

Status badge, risk knobs, backtest-edge inline stats (so the operator knows
what they're deploying), then action buttons. Designed to be dense — at a
glance you can see whether the strategy has positive train + test edge and
how many OOS trades it had.
"""
from __future__ import annotations

import streamlit as st

from core import account_manager, edge_catalog
from core.deployment import Deployment


_BADGE_BY_STATUS = {
    "idle":   ("💤", "IDLE",   "#475569"),
    "paper":  ("🟡", "PAPER",  "#b08800"),
    "live":   ("🟢", "LIVE",   "#0c8a3a"),
    "paused": ("⏸",  "PAUSED", "#aa6a00"),
    "halted": ("⛔", "HALTED", "#aa1a1a"),
}


def _badge_html(status: str) -> str:
    icon, text, color = _BADGE_BY_STATUS.get(
        status, ("?", status.upper(), "#666"))
    return (
        f"<span style='background:{color};color:white;padding:4px 10px;"
        f"border-radius:14px;font-size:0.78rem;font-weight:600;"
        f"font-family:ui-monospace,Menlo,monospace;"
        f"letter-spacing:0.05em;'>"
        f"{icon} {text}</span>"
    )


def _edge_badge_html(es: edge_catalog.EdgeStat | None) -> str:
    """Backtest-edge chip: green if survivor, amber if one-sided, red if
    negative, grey if no data."""
    if es is None:
        return ("<span style='color:#6b7280;font-size:0.78rem;"
                "font-family:ui-monospace,Menlo,monospace;'>"
                "no backtest edge cached — run sweep_grid</span>")
    pos_train = es.train_r > 0 and es.train_pf >= 1.0
    pos_test = es.test_r > 0 and es.test_pf >= 1.0
    if pos_train and pos_test:
        bg, tag = "#15803d", "EDGE"
    elif pos_test:
        bg, tag = "#a16207", "OOS only"
    elif pos_train:
        bg, tag = "#a16207", "IS only"
    else:
        bg, tag = "#7f1d1d", "negative"
    return (
        f"<span style='background:{bg};color:white;padding:3px 8px;"
        f"border-radius:6px;font-size:0.72rem;font-weight:600;"
        f"font-family:ui-monospace,Menlo,monospace;letter-spacing:0.04em;'>"
        f"{tag}</span>"
        f"<span style='color:#cbd5e1;font-size:0.78rem;margin-left:8px;"
        f"font-family:ui-monospace,Menlo,monospace;"
        f"font-variant-numeric:tabular-nums;'>"
        f"PF train <b>{es.train_pf:.2f}</b> / test <b>{es.test_pf:.2f}</b>"
        f"  ·  R train <b>{es.train_r:+.2f}</b> / test <b>{es.test_r:+.2f}</b>"
        f"  ·  n_test <b>{es.n_test}</b>"
        f"</span>"
    )


def render(*, login: int, dep: Deployment,
           on_go_live=None, on_paper=None, on_pause=None, on_remove=None,
           on_backtest=None, container=None) -> None:
    target = container or st
    e_stop = account_manager.emergency_stop_active()
    status = "halted" if (e_stop and dep.status == "live") else dep.status

    with target.container(border=True):
        title_cols = st.columns([7, 2])
        title_cols[0].markdown(
            f"#### `{dep.strategy}` × `{dep.ticker}` × `{dep.tf}` "
            + ("(long-only)" if dep.long_only else "(bidir)")
        )
        title_cols[1].markdown(_badge_html(status), unsafe_allow_html=True)

        st.markdown(
            f"<div style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.82rem;color:#9ca3af;margin-bottom:6px;'>"
            f"risk <b style='color:#e5e7eb;'>{dep.risk_pct:.2f}%</b>/trade  "
            f"·  daily cap "
            f"<b style='color:#e5e7eb;'>{dep.daily_cap_pct:.2f}%</b>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Backtest edge stats (read from docs/grid_results.md)
        try:
            strat_prefix = dep.strategy.split("_long")[0].split("_bidir")[0]
            es = edge_catalog.best_for(dep.ticker, dep.tf, strat_prefix)
        except Exception:
            es = None
        st.markdown(_edge_badge_html(es), unsafe_allow_html=True)

        # Activity meta — only show if known (no em-dash noise)
        meta_parts = []
        if dep.last_started_at_utc:
            meta_parts.append(f"started `{dep.last_started_at_utc[:16]}`")
        if dep.last_signal_at_utc:
            meta_parts.append(f"last signal `{dep.last_signal_at_utc[:16]}`")
        if meta_parts:
            st.caption("  ·  ".join(meta_parts))

        # Actions
        act = st.columns(5)
        if act[0].button("📊 Backtest",
                          key=f"bt_{login}_{dep.deployment_id}",
                          use_container_width=True):
            if on_backtest:
                on_backtest(dep)
        if act[1].button("📡 Paper",
                          key=f"pp_{login}_{dep.deployment_id}",
                          use_container_width=True,
                          disabled=(dep.status == "live")):
            if on_paper:
                on_paper(dep)
        if act[2].button("🚀 Go Live",
                          key=f"gl_{login}_{dep.deployment_id}",
                          type="primary",
                          use_container_width=True,
                          disabled=e_stop or dep.status == "live"):
            if on_go_live:
                on_go_live(dep)
        pause_label = "▶ Resume" if dep.status == "paused" else "⏸ Pause"
        if act[3].button(pause_label,
                          key=f"pa_{login}_{dep.deployment_id}",
                          use_container_width=True,
                          disabled=(dep.status == "idle")):
            if on_pause:
                on_pause(dep)
        if act[4].button("🗑 Remove",
                          key=f"rm_{login}_{dep.deployment_id}",
                          use_container_width=True):
            if on_remove:
                on_remove(dep)
