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
    s = max(0, int(td_seconds))
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


def _parse_original_baseline(account_type: str | None) -> float:
    """challenge_100k → 100_000. Defaults to 100k for unknown types."""
    t = (account_type or "").lower()
    for tok in t.split("_"):
        if tok.endswith("k") and tok[:-1].isdigit():
            return float(tok[:-1]) * 1000.0
    return 100_000.0


def render(*, summary: StatementSummary, clock: FtmoClock,
           user_tz_name: str, account_type: str | None = None,
           total_loss_cap_pct: float = 10.0,
           container=None) -> None:
    target = container or st
    target.markdown("### 📜  Statement (today)")

    cols = target.columns(4)
    cols[0].metric("Realized today",
                     f"${summary.realized_pnl_today:+,.2f}",
                     delta=None)
    cols[1].metric("Unrealized",
                     f"${summary.unrealized_pnl:+,.2f}",
                     delta=None)
    cols[2].metric("Realized total",
                     f"${summary.realized_pnl_total:+,.2f}",
                     delta=None)
    cols[3].metric("Trades today", f"{summary.n_trades_today}",
                     delta=f"{summary.n_trades_total} total"
                     if summary.n_trades_total else None)

    # ── Buffer cards: Total-loss is ALWAYS computed against the original
    # FTMO baseline (parsed from account_type, e.g. challenge_100k → 100k),
    # NOT the user's re-anchored personal baseline. This matches the
    # KPI strip behaviour and is the truth FTMO actually enforces.
    original_baseline = _parse_original_baseline(account_type)
    original_cap_dollars = original_baseline * total_loss_cap_pct / 100.0
    eq = summary.current_equity
    all_time_loss = max(0.0, original_baseline - eq) if eq else 0.0
    total_remaining_truth = max(0.0, original_cap_dollars - all_time_loss)

    target.markdown("")
    cols2 = target.columns(2)
    daily_remain_color = ("#16a34a" if summary.daily_loss_remaining
                          > summary.starting_equity * 0.02 else "#f59e0b")
    cols2[0].markdown(
        f"<div style='background:#0f1419;border:1px solid #1f2937;"
        f"border-radius:8px;padding:14px;'>"
        f"<div style='color:#9ca3af;font-size:0.74rem;"
        f"text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;'>"
        f"Daily-loss buffer</div>"
        f"<div style='color:{daily_remain_color};font-size:1.4rem;"
        f"font-weight:600;font-family:ui-monospace,Menlo,monospace;"
        f"font-variant-numeric:tabular-nums;'>"
        f"${summary.daily_loss_remaining:,.2f}</div>"
        f"<div style='color:#9ca3af;font-size:0.74rem;'>remaining today</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    used_pct = (all_time_loss / original_cap_dollars * 100.0
                 if original_cap_dollars > 0 else 0.0)
    total_remain_color = ("#16a34a" if used_pct < 60
                          else "#f59e0b" if used_pct < 80 else "#dc2626")
    cols2[1].markdown(
        f"<div style='background:#0f1419;border:1px solid #1f2937;"
        f"border-radius:8px;padding:14px;'>"
        f"<div style='color:#9ca3af;font-size:0.74rem;"
        f"text-transform:uppercase;letter-spacing:0.08em;margin-bottom:4px;'>"
        f"Total-loss buffer</div>"
        f"<div style='color:{total_remain_color};font-size:1.4rem;"
        f"font-weight:600;font-family:ui-monospace,Menlo,monospace;"
        f"font-variant-numeric:tabular-nums;'>"
        f"${total_remaining_truth:,.2f}</div>"
        f"<div style='color:#9ca3af;font-size:0.74rem;'>"
        f"${all_time_loss:,.0f} lost · {used_pct:.0f}% of "
        f"${original_cap_dollars:,.0f} cap "
        f"({total_loss_cap_pct:.0f}% of ${original_baseline:,.0f})</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # FTMO close countdown
    target.markdown("")
    now = datetime.now(timezone.utc)
    next_pre = clock.next_pre_close_utc(now)
    secs = (next_pre - now).total_seconds()
    next_local = clock.next_pre_close_user_local(now)
    target.markdown(
        f"🕒 **Next FTMO close:** "
        f"`{next_local.strftime('%H:%M %Z %Y-%m-%d')}` "
        f"({user_tz_name}) — in `{_humanize(secs)}`"
    )

    if summary.last_deal_at_utc:
        target.caption(f"Last broker deal: `{summary.last_deal_at_utc[:19]}`")
