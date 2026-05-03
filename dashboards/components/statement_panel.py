"""
statement_panel.py — today's realized + unrealized + daily-loss buffer +
countdown to next FTMO daily-close in user's local tz.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core.account_statement import StatementSummary
from core.ftmo_clock import FtmoClock


def _humanize(td_seconds: float) -> str:
    s = int(td_seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h {mm}m"
    d, hh = divmod(h, 24)
    return f"{d}d {hh}h"


def render(*, summary: StatementSummary, clock: FtmoClock, user_tz_name: str,
            container=None) -> None:
    target = container or st
    target.markdown("### 📜  Statement (today)")

    cols = target.columns(4)
    cols[0].metric("Realized today", f"${summary.realized_pnl_today:+,.2f}")
    cols[1].metric("Unrealized", f"${summary.unrealized_pnl:+,.2f}")
    cols[2].metric("Realized total", f"${summary.realized_pnl_total:+,.2f}")
    cols[3].metric("Trades today", f"{summary.n_trades_today}")

    cols2 = target.columns(2)
    cols2[0].markdown(
        f"**Daily-loss buffer:** ${summary.daily_loss_remaining:,.0f} remaining"
    )
    cols2[1].markdown(
        f"**Total-loss buffer:** ${summary.total_loss_remaining:,.0f} remaining"
    )

    # FTMO close countdown
    now = datetime.now(timezone.utc)
    next_pre = clock.next_pre_close_utc(now)
    secs = (next_pre - now).total_seconds()
    next_local = clock.next_pre_close_user_local(now)
    target.markdown(
        f"🕒 **Next FTMO close:** "
        f"`{next_local.strftime('%H:%M %Z')}` "
        f"({user_tz_name}) — in `{_humanize(secs)}`"
    )

    if summary.last_deal_at_utc:
        target.caption(f"Last broker deal: `{summary.last_deal_at_utc[:19]}`")
