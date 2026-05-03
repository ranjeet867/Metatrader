"""
time_guard_status.py — countdown widgets for the next forced flat.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core.time_guards import (
    TimeGuardCfg,
    next_daily_flat_at,
    next_weekend_flat_at,
)


def _humanize(td_seconds: float) -> str:
    if td_seconds < 60:
        return f"{int(td_seconds)}s"
    minutes = td_seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60
    if hours < 24:
        h, m = int(hours), int(minutes - int(hours) * 60)
        return f"{h}h {m}m"
    days = hours / 24
    d, h = int(days), int(hours - int(days) * 24)
    return f"{d}d {h}h"


def render_countdowns(cfg: TimeGuardCfg, *, container=None) -> None:
    """Render two countdown lines: next weekend flat + next daily flat."""
    target = container or st
    now = datetime.now(timezone.utc)
    try:
        next_we = next_weekend_flat_at(now, cfg)
        we_in = (next_we - now).total_seconds()
        target.markdown(
            f"🕒 **Weekend flat** in `{_humanize(we_in)}` "
            f"({next_we.strftime('%Y-%m-%d %H:%M')} UTC)"
        )
    except Exception:
        target.caption("(could not compute weekend flat schedule)")
    try:
        next_d = next_daily_flat_at(now, cfg)
        d_in = (next_d - now).total_seconds()
        target.markdown(
            f"🕒 **Daily flat (stocks/indices)** in `{_humanize(d_in)}` "
            f"({next_d.strftime('%Y-%m-%d %H:%M')} UTC)"
        )
    except Exception:
        target.caption("(could not compute daily flat schedule)")
