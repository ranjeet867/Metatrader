"""
active_runs_panel.py — what is ACTUALLY running on this account right now.

Source of truth: deployments.json (status field) plus the journal +
trades table for run-level stats. The panel does not start runs — that's
the deployment-card buttons' job. This panel just visualises the
current state and gives a Stop button per run.

Design goals:
  • Every running deployment is one row, clearly tagged 🟡 PAPER or 🟢 LIVE.
  • Show exactly what the operator wants to know mid-session:
      - last signal time
      - trades closed today
      - realized P&L today
      - unrealized P&L (sum of open paper positions for this slot)
  • One-click Stop sets status back to idle and writes a journal entry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from core import account_manager, deployment as dep_mod, storage
from core.deployment import Deployment


def _trades_for_deployment(db_path: Path, dep: Deployment,
                            *, since_utc: datetime | None = None
                            ) -> pd.DataFrame:
    """Pull trades joined to the deployment by (strategy, symbol, tf)."""
    if not Path(db_path).exists():
        return pd.DataFrame()
    try:
        with storage.connect(db_path) as c:
            q = ("SELECT closed_at_utc, opened_at_utc, mode, strategy, "
                 "symbol, tf, direction, entry_price, exit_price, "
                 "realized_pnl, r_multiple, close_reason FROM trades "
                 "WHERE strategy = ? AND symbol = ? AND tf = ? "
                 "AND realized_pnl IS NOT NULL")
            args = [dep.strategy, dep.ticker, dep.tf]
            if since_utc is not None:
                q += " AND closed_at_utc >= ?"
                args.append(since_utc.isoformat())
            q += " ORDER BY closed_at_utc DESC"
            rows = c.execute(q, args).fetchall()
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows, columns=[
            "closed_at_utc", "opened_at_utc", "mode", "strategy",
            "symbol", "tf", "direction", "entry", "exit",
            "realized_pnl", "r_multiple", "close_reason",
        ])
    except Exception:
        return pd.DataFrame()


def _ftmo_day_start(now_utc: datetime) -> datetime:
    """Most recent 22:00 UTC at-or-before now."""
    cand = now_utc.replace(hour=22, minute=0, second=0, microsecond=0)
    if cand > now_utc:
        cand = cand.replace(day=cand.day - 1) if cand.day > 1 else (
            cand.replace(month=cand.month - 1, day=28))
    return cand


def render(*, login: int, container=None) -> None:
    target = container or st
    deployments = dep_mod.load_deployments(login)
    running = [d for d in deployments if d.status in ("paper", "live")]

    target.markdown("### ▶  Active runs")
    target.caption(
        "Every paper or live run for this account. Rows refresh on every "
        "page load. Click **Stop** to flip back to idle.")

    if not running:
        target.info(
            "No active runs. Use the **Deployments** tab to start one.")
        return

    db_path = account_manager.get_db_path(login)
    now = datetime.now(timezone.utc)
    day_start = _ftmo_day_start(now)

    # Header row
    h = target.columns([3, 1, 1, 1, 1, 1, 1, 1])
    headers = ["Deployment", "Mode", "Risk%",
                "Started", "Trades today", "Realized today",
                "Last close", "Action"]
    for col, label in zip(h, headers):
        col.markdown(
            f"<div style='color:#9ca3af;font-size:0.72rem;"
            f"text-transform:uppercase;letter-spacing:0.06em;'>"
            f"{label}</div>",
            unsafe_allow_html=True)

    for d in running:
        trades = _trades_for_deployment(db_path, d, since_utc=day_start)
        n_today = len(trades)
        realized_today = float(trades["realized_pnl"].sum()) if n_today else 0.0
        last_close = (str(trades["closed_at_utc"].iloc[0])[:16]
                       if n_today else "—")

        row = target.columns([3, 1, 1, 1, 1, 1, 1, 1])
        with row[0].container(border=True):
            mode_color = {"paper": "#b08800", "live": "#0c8a3a"}.get(
                d.status, "#475569")
            mode_dot = ("🟡" if d.status == "paper"
                         else "🟢" if d.status == "live" else "⚪")
            row[0].markdown(
                f"<div style='font-family:ui-monospace,Menlo,monospace;"
                f"font-size:0.86rem;line-height:1.4;'>"
                f"<b>{mode_dot} {d.strategy}</b><br>"
                f"<span style='color:#9ca3af;'>{d.ticker} · {d.tf} · "
                f"{'long' if d.long_only else 'bidir'}</span></div>",
                unsafe_allow_html=True)
        row[1].markdown(
            f"<span style='background:{mode_color};color:white;"
            f"padding:2px 8px;border-radius:10px;font-size:0.7rem;"
            f"font-weight:600;'>{d.status.upper()}</span>",
            unsafe_allow_html=True)
        row[2].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.85rem;'>{d.risk_pct:.2f}%</span>",
            unsafe_allow_html=True)
        row[3].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.78rem;color:#9ca3af;'>"
            f"{d.last_started_at_utc[:16] if d.last_started_at_utc else '—'}"
            f"</span>", unsafe_allow_html=True)
        row[4].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;'>"
            f"{n_today}</span>", unsafe_allow_html=True)
        pnl_color = ("#16a34a" if realized_today >= 0
                      else "#dc2626") if n_today else "#9ca3af"
        row[5].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"color:{pnl_color};font-weight:600;'>"
            f"${realized_today:+,.2f}</span>", unsafe_allow_html=True)
        row[6].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.78rem;color:#9ca3af;'>{last_close}</span>",
            unsafe_allow_html=True)
        if row[7].button("⏹ Stop", key=f"stop_run_{d.deployment_id}",
                          use_container_width=True):
            dep_mod.update_status(login, d.deployment_id, "idle")
            st.toast(f"Stopped {d.deployment_id}")
            st.rerun()
