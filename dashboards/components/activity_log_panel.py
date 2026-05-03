"""
activity_log_panel.py — recent operational events for an account.

Pulls from the bridge_events table (where we already record
ui_go_live_confirmed, emergency_stop_cleared, manual_close_in_mt5,
close_all_ui, etc.) plus the trades table to build a unified time line.

The panel exists so the operator never has to wonder "did my click
actually do anything?" — every action lands here within a second.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from core import account_manager, storage


_EVENT_ICON = {
    "ui_go_live_confirmed":     "🚀",
    "emergency_stop_cleared":    "✅",
    "emergency_stop_activated":  "⛔",
    "manual_close_ui":           "✋",
    "manual_close_in_mt5":       "📋",
    "close_all_ui":              "🛑",
    "ftmo_pre_close_auto":       "🕒",
    "phantom_open_detected":     "⚠",
    "deploy_paper":              "📡",
    "deploy_live":               "🚀",
    "stop_run":                  "⏹",
    "remove_deployment":         "🗑",
    "trade_open":                "📈",
    "trade_close":               "📉",
}


def _load_events(db_path: Path, *, hours: int) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with storage.connect(db_path) as c:
            rows = c.execute(
                "SELECT pinged_at_utc, method, ok, error "
                "FROM bridge_events WHERE pinged_at_utc >= ? "
                "ORDER BY ROWID DESC LIMIT 500",
                (since,),
            ).fetchall()
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame(columns=["time", "kind", "ok", "detail"])
    return pd.DataFrame(rows, columns=["time", "kind", "ok", "detail"])


def _load_recent_trades(db_path: Path, *, hours: int) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with storage.connect(db_path) as c:
            rows = c.execute(
                "SELECT closed_at_utc, mode, strategy, symbol, tf, "
                "direction, realized_pnl, r_multiple, close_reason "
                "FROM trades WHERE closed_at_utc >= ? "
                "AND realized_pnl IS NOT NULL ORDER BY ROWID DESC LIMIT 200",
                (since,),
            ).fetchall()
    except Exception:
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=[
        "time", "mode", "strategy", "symbol", "tf",
        "direction", "pnl", "r", "reason",
    ])


def render(*, login: int, hours: int = 24, container=None) -> None:
    target = container or st
    target.markdown("### 📜  Activity log")
    db = account_manager.get_db_path(login)

    cols = target.columns([2, 1])
    cols[0].caption(
        f"Recent operational events from `data/accounts/{login}/v2.db` "
        f"(last {hours}h). Updated on every page load."
    )
    hours_chosen = int(cols[1].selectbox(
        "window",
        options=[1, 4, 12, 24, 72, 168],
        index=3,
        format_func=lambda h: (f"{h}h" if h < 48
                                 else f"{h//24}d"),
        key=f"actlog_hours_{login}",
        label_visibility="collapsed",
    ))
    if hours_chosen != hours:
        hours = hours_chosen

    events = _load_events(db, hours=hours)
    trades = _load_recent_trades(db, hours=hours)

    # Merge into a single timeline
    rows: list[dict] = []
    for _, e in events.iterrows():
        kind = str(e["kind"])
        icon = _EVENT_ICON.get(kind, "•")
        rows.append({
            "time": str(e["time"])[:19],
            "icon": icon,
            "what": kind,
            "ok": "✓" if e["ok"] else "✗",
            "detail": str(e["detail"] or ""),
        })
    for _, t in trades.iterrows():
        pnl = float(t["pnl"])
        icon = "📈" if pnl >= 0 else "📉"
        rows.append({
            "time": str(t["time"])[:19],
            "icon": icon,
            "what": f"trade_close ({t['mode']})",
            "ok": "✓" if pnl >= 0 else "✗",
            "detail": (f"{t['strategy']} {t['symbol']} {t['tf']} "
                        f"{t['direction']} → ${pnl:+,.2f} "
                        f"(R {t['r']:+.2f}) [{t['reason']}]"),
        })

    if not rows:
        target.caption("(no events in this window)")
        return

    rows.sort(key=lambda r: r["time"], reverse=True)
    df = pd.DataFrame(rows[:100])

    # Custom styled rendering — st.dataframe doesn't support per-cell colors easily
    for r in rows[:100]:
        target.markdown(
            f"<div style='display:flex;gap:14px;padding:6px 4px;"
            f"border-bottom:1px solid #1f2937;"
            f"font-family:ui-monospace,Menlo,monospace;font-size:0.82rem;"
            f"font-variant-numeric:tabular-nums;'>"
            f"<span style='color:#6b7280;min-width:140px;'>{r['time']}</span>"
            f"<span>{r['icon']}</span>"
            f"<span style='color:#e5e7eb;min-width:200px;'>{r['what']}</span>"
            f"<span style='color:{'#16a34a' if r['ok']=='✓' else '#dc2626'};"
            f"min-width:20px;'>{r['ok']}</span>"
            f"<span style='color:#9ca3af;'>{r['detail']}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
