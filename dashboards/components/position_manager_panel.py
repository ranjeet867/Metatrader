"""
position_manager_panel.py — open positions table + Close One / Close All.

Dense layout: a styled DataFrame with R-multiple gradient colouring, a
typed-confirm Close-All button up top, and per-row Close button stamped
inline. INVARIANT-12 (no auto-close phantoms) and INVARIANT-13 (idempotent
reconcile) visible — phantom positions are surfaced with a yellow warning
but never auto-closed.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core.position_manager import PositionManager


CLOSE_ALL_PHRASE = "CLOSE ALL"


def _r_gradient(r: float | None) -> str:
    """Hex colour string proportional to R-multiple. None / non-numeric → grey."""
    if r is None:
        return "#374151"
    if r >= 1.0:
        return "#15803d"
    if r >= 0:
        return "#166534"
    if r >= -0.5:
        return "#7c2d12"
    return "#7f1d1d"


def _style_pnl(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "color: #9ca3af;"
    if f > 0:
        return "color: #16a34a; font-weight: 600;"
    if f < 0:
        return "color: #dc2626; font-weight: 600;"
    return "color: #9ca3af;"


def render(*, pm: PositionManager, container=None) -> None:
    target = container or st
    target.markdown("### 💼  Position manager")

    try:
        positions = pm.list_open()
    except Exception as e:
        msg = str(e)
        if "unknown method" in msg or "not implemented" in msg.lower():
            target.warning(
                "ℹ Your MT5 bridge EA doesn't yet implement the position "
                "methods (`positions_get`, `position_close`, "
                "`history_deals_get`). Upgrade the bridge EA to enable "
                "live position management. Until then this panel is "
                "read-only — open a trade in MT5 directly to test.")
            target.caption(
                "See `docs/RUNBOOK.md` → 'Upgrading the MT5 bridge for "
                "Phase 2.5 position management'.")
        else:
            target.error(f"Could not query broker: {msg}")
        return

    # Reconcile to detect manual closes / phantoms (idempotent).
    try:
        report = pm.reconcile_with_broker()
        if report.n_phantom > 0:
            target.warning(
                f"⚠ {report.n_phantom} phantom position(s) on broker that "
                f"we don't track: {sorted(report.phantom_tickets)} — "
                f"NOT auto-closed."
            )
        if report.n_manual_closes_recorded > 0:
            target.info(
                f"📋 Detected {report.n_manual_closes_recorded} manual close"
                f"(s) in MT5 since last refresh — synthesised into journal."
            )
    except Exception as e:
        target.caption(f"(reconcile failed: {e})")

    if not positions:
        target.caption("no open positions")
        return

    n = len(positions)
    unreal = sum(p.unrealized_pnl for p in positions)
    avg_r = (sum((p.unrealized_r or 0.0) for p in positions) / n) if n else 0.0

    # Header row: count + sums + Close-All popover
    head = target.columns([3, 1])
    head[0].markdown(
        f"<div style='font-family:ui-monospace,Menlo,monospace;"
        f"font-size:0.92rem;'>"
        f"<b>{n}</b> open  ·  unrealized "
        f"<b style='color:{'#16a34a' if unreal >= 0 else '#dc2626'};'>"
        f"${unreal:+,.2f}</b>"
        f"  ·  avg R "
        f"<b style='color:{'#16a34a' if avg_r >= 0 else '#dc2626'};'>"
        f"{avg_r:+.2f}</b></div>",
        unsafe_allow_html=True,
    )
    with head[1].popover("⛔  Close ALL", use_container_width=True):
        st.markdown(
            f"Type `{CLOSE_ALL_PHRASE}` exactly to confirm closing every "
            f"open position on this account.")
        confirm = st.text_input("confirm", value="",
                                key="pm_closeall_confirm",
                                label_visibility="collapsed")
        if st.button("Close all positions",
                      disabled=(confirm != CLOSE_ALL_PHRASE),
                      type="primary", key="pm_closeall_btn"):
            results = pm.close_all(reason="close_all_ui")
            ok_n = sum(1 for r in results if r.ok)
            st.success(
                f"Closed {ok_n}/{len(results)} positions. "
                f"{len(results) - ok_n} failures.")
            st.rerun()

    # Build table
    rows = []
    for p in positions:
        rows.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "side": p.direction.upper(),
            "lots": p.lots,
            "entry": round(p.entry_price, 5),
            "now": round(p.current_price, 5),
            "stop": round(p.stop_price, 5) if p.stop_price else None,
            "target": round(p.target_price, 5) if p.target_price else None,
            "$ pnl": round(p.unrealized_pnl, 2),
            "R": (round(p.unrealized_r, 2)
                  if p.unrealized_r is not None else None),
            "strategy": p.strategy_name or "—",
        })
    df = pd.DataFrame(rows)
    styled = (
        df.style
        .map(_style_pnl, subset=["$ pnl", "R"])
        .format({"entry": "{:.5f}", "now": "{:.5f}",
                 "stop": "{:.5f}", "target": "{:.5f}",
                 "$ pnl": "${:+,.2f}", "R": "{:+.2f}"}, na_rep="—")
    )
    target.dataframe(styled, use_container_width=True,
                       height=min(420, 36 * (n + 1)))

    # Per-row Close — inline buttons with the position context next to them.
    target.markdown("**Close one:**")
    for p in positions:
        c = target.columns([3, 2, 1, 1])
        c[0].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.85rem;'>"
            f"<code>{p.ticket}</code> · {p.symbol} · "
            f"{p.direction.upper()} {p.lots} lots @ {p.entry_price}"
            f"</span>", unsafe_allow_html=True)
        c[1].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.85rem;color:"
            f"{'#16a34a' if p.unrealized_pnl >= 0 else '#dc2626'};'>"
            f"PnL ${p.unrealized_pnl:+,.2f}"
            + (f"  ·  R {p.unrealized_r:+.2f}"
               if p.unrealized_r is not None else "")
            + "</span>", unsafe_allow_html=True)
        if c[2].button("Close", key=f"pm_close_{p.ticket}",
                          type="secondary", use_container_width=True):
            res = pm.close_one(p.ticket, reason="manual_close_ui")
            if res.ok:
                st.toast(f"Closed #{p.ticket} (${res.realized_pnl:+,.2f})")
            else:
                st.toast(f"Close failed: {res.error}", icon="⛔")
            st.rerun()
