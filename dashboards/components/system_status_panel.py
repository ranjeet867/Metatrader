"""
system_status_panel.py — Operations-page status strip.

Surfaces three production-grade safety signals:
  • Circuit breaker state (OK / STOP_NEW / HALT) + reasons
  • Open-position collisions across deployments (strict-policy view)
  • Quick links to fix or override

This panel renders in `dashboards/pages/0_🚀_Operations.py` directly
under the KPI strip. It is read-only — does NOT call enforce(),
because the UI is rerendered on every interaction. The runner does
the enforcement.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from core import circuit_breaker as cb
from core import position_guard as pg


_STATE_BADGE = {
    "OK":       ("✅", "#16a34a"),
    "STOP_NEW": ("🟡", "#fbbf24"),
    "HALT":     ("⛔", "#dc2626"),
}


def render(*, login: int, db_path: Path, mode: str = "live",
              open_positions: list[pg.OpenPosition] | None = None) -> None:
    """Render the system-status strip on the Operations page.

    Args:
      login: account login — selects per-account circuit-breaker config.
      db_path: account DB; used to read realised PnL for breaker state.
      mode: 'live' (default) or 'paper'. The breaker runs separately
            per mode — a paper bust doesn't affect the live tracker.
      open_positions: live snapshot of OpenPosition across all
            deployments on this account; used for collision detection.
            Pass [] when bridge is offline.
    """
    cfg = cb.load_config(login)
    open_positions = open_positions or []
    status = cb.evaluate(
        db_path, mode=mode, cfg=cfg,
        open_positions_count=len(open_positions),
    )
    icon, color = _STATE_BADGE[status.state]

    with st.container(border=True):
        cols = st.columns([2, 2, 2, 2, 3])

        # State badge
        cols[0].markdown(
            f"<div style='font-size: 0.85rem; color: #9ca3af;'>"
            f"Circuit breaker</div>"
            f"<div style='font-size: 1.4rem; font-weight: 700; color: {color};'>"
            f"{icon} {status.state}</div>",
            unsafe_allow_html=True,
        )

        # Realised today
        today_color = ("#16a34a" if status.realised_today >= 0
                          else "#dc2626")
        cols[1].markdown(
            f"<div style='font-size: 0.85rem; color: #9ca3af;'>Today</div>"
            f"<div style='font-size: 1.2rem; font-weight: 700; "
            f"color: {today_color};'>${status.realised_today:+,.2f}</div>"
            f"<div style='font-size: 0.7rem; color: #6b7280;'>"
            f"daily limit ${cfg.daily_loss_dollars or 0:,.0f}</div>",
            unsafe_allow_html=True,
        )

        # Realised lifetime
        total_color = ("#16a34a" if status.realised_total >= 0
                          else "#dc2626")
        cols[2].markdown(
            f"<div style='font-size: 0.85rem; color: #9ca3af;'>Lifetime "
            f"({mode})</div>"
            f"<div style='font-size: 1.2rem; font-weight: 700; "
            f"color: {total_color};'>${status.realised_total:+,.2f}</div>"
            f"<div style='font-size: 0.7rem; color: #6b7280;'>"
            f"total floor ${cfg.total_loss_dollars or 0:,.0f}</div>",
            unsafe_allow_html=True,
        )

        # Streak
        streak_color = ("#dc2626" if status.consec_losses >= 3
                          else "#fbbf24" if status.consec_losses >= 1
                          else "#9ca3af")
        cols[3].markdown(
            f"<div style='font-size: 0.85rem; color: #9ca3af;'>Loss streak</div>"
            f"<div style='font-size: 1.2rem; font-weight: 700; "
            f"color: {streak_color};'>{status.consec_losses}</div>"
            f"<div style='font-size: 0.7rem; color: #6b7280;'>"
            f"max {cfg.max_consec_losses or 0}</div>",
            unsafe_allow_html=True,
        )

        # Open positions + collisions
        collisions = pg.collisions_in_open_set(open_positions)
        col_color = ("#dc2626" if collisions else "#9ca3af")
        cols[4].markdown(
            f"<div style='font-size: 0.85rem; color: #9ca3af;'>Open positions"
            f"</div>"
            f"<div style='font-size: 1.2rem; font-weight: 700; "
            f"color: {col_color};'>{len(open_positions)}"
            f"{(' &nbsp; ⚠ ' + str(len(collisions)) + ' collision') if collisions else ''}"
            f"</div>"
            f"<div style='font-size: 0.7rem; color: #6b7280;'>"
            f"max {cfg.max_open_positions or 0}</div>",
            unsafe_allow_html=True,
        )

        # Reasons row (full width) when not OK
        if not status.is_ok:
            banner_color = ("#dc2626" if status.state == "HALT"
                             else "#fbbf24")
            reasons_html = "<br>".join(f"• {r}" for r in status.reasons)
            st.markdown(
                f"<div style='margin-top: 0.5rem; padding: 0.6rem 0.9rem; "
                f"background: rgba({'220,38,38' if status.state == 'HALT' else '251,191,36'},"
                f"0.10); border-left: 3px solid {banner_color}; "
                f"border-radius: 4px; color: {banner_color}; "
                f"font-size: 0.88rem;'>"
                f"<b>{status.state}:</b> the runner is "
                f"{'REFUSING all signals' if status.state == 'HALT' else 'allowing existing positions to ride to SL/TP but blocking new opens'}.<br>"
                f"{reasons_html}"
                f"</div>",
                unsafe_allow_html=True,
            )

        if collisions:
            details = "<br>".join(
                f"• `{a.deployment_id}` and `{b.deployment_id}` both hold "
                f"a position on `{a.symbol}` ({a.side}/{b.side})"
                for a, b in collisions
            )
            st.markdown(
                f"<div style='margin-top: 0.5rem; padding: 0.6rem 0.9rem; "
                f"background: rgba(220,38,38,0.10); "
                f"border-left: 3px solid #dc2626; border-radius: 4px; "
                f"color: #dc2626; font-size: 0.88rem;'>"
                f"<b>POSITION COLLISIONS:</b> two or more deployments hold "
                f"positions on the same symbol — duplicate signal sources, "
                f"double exposure. Pause or remove one of each pair.<br>"
                f"{details}"
                f"</div>",
                unsafe_allow_html=True,
            )

    # Quick toggle row — link to Settings to edit thresholds
    if not status.is_ok:
        st.caption(
            "💡 Edit circuit-breaker thresholds in the **🛠 Settings** "
            "tab below. The runner applies them on the next tick."
        )
