"""
position_manager_panel.py — open positions table + Close One / Close All.

INVARIANT-12 + INVARIANT-13 visible: phantom positions show ⚠ but NEVER
auto-close; reconcile is invoked after every action.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from core import account_manager
from core.position_manager import LivePosition, PositionManager


CLOSE_ALL_PHRASE = "CLOSE ALL"


def render(*, pm: PositionManager, container=None) -> None:
    target = container or st
    target.markdown("### 📋  Position Manager")
    try:
        positions = pm.list_open()
    except Exception as e:
        target.error(f"Could not query broker: {e}")
        return

    # Reconcile to detect manual closes / phantoms (idempotent — safe to
    # call every render).
    try:
        report = pm.reconcile_with_broker()
        if report.n_phantom > 0:
            target.warning(
                f"⚠ Phantom positions detected (broker has them, we don't): "
                f"{list(report.phantom_tickets)}"
            )
        if report.n_manual_closes_recorded > 0:
            target.info(
                f"📋 Detected {report.n_manual_closes_recorded} manual close(s) "
                f"in MT5 since last refresh."
            )
    except Exception as e:
        target.caption(f"(reconcile failed: {e})")

    if not positions:
        target.caption("(no open positions)")
        return

    n = len(positions)
    unreal = sum(p.unrealized_pnl for p in positions)
    cols = target.columns([2, 1])
    cols[0].markdown(f"**Open positions:** {n}  ·  "
                       f"**Unrealized:** ${unreal:+,.2f}")
    # Close-all (typed-confirm)
    with cols[1].popover("⛔  Close ALL"):
        st.markdown(f"Type `{CLOSE_ALL_PHRASE}` exactly:")
        confirm = st.text_input("", value="", key="pm_closeall_confirm",
                                  label_visibility="collapsed")
        if st.button("Close all positions",
                          disabled=(confirm != CLOSE_ALL_PHRASE),
                          type="primary", key="pm_closeall_btn"):
            results = pm.close_all(reason="close_all_ui")
            ok_n = sum(1 for r in results if r.ok)
            st.success(
                f"Closed {ok_n}/{len(results)} positions. "
                f"{len(results) - ok_n} failures."
            )
            st.rerun()

    # Table with per-row Close button
    rows = []
    for p in positions:
        rows.append({
            "ticket": p.ticket, "symbol": p.symbol,
            "dir": p.direction, "lots": p.lots,
            "entry": round(p.entry_price, 5),
            "now": round(p.current_price, 5),
            "stop": round(p.stop_price, 5) if p.stop_price else "—",
            "$_pnl": round(p.unrealized_pnl, 2),
            "R": (round(p.unrealized_r, 2) if p.unrealized_r is not None else "—"),
            "strategy": p.strategy_name or "—",
        })
    target.dataframe(pd.DataFrame(rows), use_container_width=True,
                       height=min(420, 36 * (n + 1)))

    target.markdown("**Close one:**")
    for p in positions:
        cls = target.columns([2, 1, 1, 1])
        cls[0].markdown(f"`{p.ticket}` · `{p.symbol}` · {p.direction.upper()} "
                          f"{p.lots} lots @ {p.entry_price}")
        cls[1].markdown(f"PnL: ${p.unrealized_pnl:+,.2f}")
        if cls[2].button("Close", key=f"pm_close_{p.ticket}",
                              type="secondary"):
            res = pm.close_one(p.ticket, reason="manual_close_ui")
            if res.ok:
                st.toast(f"Closed #{p.ticket} (${res.realized_pnl:+,.2f})")
            else:
                st.toast(f"Close failed: {res.error}", icon="⛔")
            st.rerun()
