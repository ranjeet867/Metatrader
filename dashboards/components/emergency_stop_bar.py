"""
emergency_stop_bar.py — sticky banner at the top of Operations.

When data/EMERGENCY_STOP exists, every live order is refused. The banner
provides a typed-confirm clear button so the operator can lift the stop
without dropping to a terminal.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core import account_manager


CLEAR_PHRASE = "CLEAR EMERGENCY_STOP"


def render(*, container=None) -> None:
    target = container or st
    active = account_manager.emergency_stop_active()
    cols = target.columns([4, 1])
    if active:
        cols[0].error(
            "⛔  **EMERGENCY STOP active** — all live orders are refused. "
            f"Type `{CLEAR_PHRASE}` exactly to clear:",
            icon="⛔",
        )
        confirm = cols[0].text_input(
            "type clearance phrase",
            value="", key="estop_confirm_input",
            label_visibility="collapsed",
        )
        if cols[1].button("✅ Clear",
                              type="primary", key="estop_clear_btn",
                              disabled=(confirm != CLEAR_PHRASE)):
            account_manager.clear_emergency_stop()
            # Audit
            try:
                from core.journal import JournalWriter
                # Best-effort: log against active account's DB if one set
                from dashboards.components.account_switcher import (
                    get_active_login,
                )
                lg = get_active_login()
                if lg is not None:
                    j = JournalWriter(account_manager.get_db_path(lg))
                    j.record_bridge_event(
                        method="emergency_stop_cleared",
                        latency_ms=0, ok=True,
                        error=f"by=ui at_utc={datetime.now(timezone.utc).isoformat()}",
                    )
            except Exception:
                pass
            st.toast("EMERGENCY_STOP cleared")
            st.rerun()
    else:
        cols[0].success("🟢  Emergency Stop inactive — live trading allowed.",
                          icon="🟢")
        if cols[1].button("⛔  Activate", type="secondary",
                              key="estop_activate_btn"):
            account_manager.touch_emergency_stop()
            st.toast("EMERGENCY_STOP activated")
            st.rerun()
