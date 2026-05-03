"""
ftmo_progress.py — animated FTMO compliance progress bars.

Daily loss / total loss / profit-target / days-traded.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st


def render(*, account_login: int, baseline_equity: float, current_equity: float,
            realized_today: float, unrealized: float,
            daily_cap_pct: float = 5.0, total_cap_pct: float = 10.0,
            profit_target_pct: float = 10.0,
            days_traded: int = 0, days_required: int = 5,
            container=None):
    target = container or st
    daily_cap_dollars = baseline_equity * daily_cap_pct / 100.0
    total_cap_dollars = baseline_equity * total_cap_pct / 100.0
    profit_target_dollars = baseline_equity * profit_target_pct / 100.0

    day_loss = max(0.0, -(realized_today + unrealized))
    daily_used_pct = min(100.0, day_loss / daily_cap_dollars * 100.0
                            if daily_cap_dollars > 0 else 0)
    all_time_loss = max(0.0, baseline_equity - current_equity)
    total_used_pct = min(100.0, all_time_loss / total_cap_dollars * 100.0
                            if total_cap_dollars > 0 else 0)
    profit = max(0.0, current_equity - baseline_equity)
    profit_pct = min(100.0, profit / profit_target_dollars * 100.0
                       if profit_target_dollars > 0 else 0)

    target.markdown("**FTMO compliance**")

    target.markdown(f"Daily loss vs {daily_cap_pct:.1f}% cap "
                      f"— ${day_loss:,.0f} of ${daily_cap_dollars:,.0f}")
    target.progress(daily_used_pct / 100.0, text=f"{daily_used_pct:.0f}%")
    if daily_used_pct >= 80:
        target.error("⚠ Above 80% of daily cap")

    target.markdown(f"Total loss vs {total_cap_pct:.1f}% cap "
                      f"— ${all_time_loss:,.0f} of ${total_cap_dollars:,.0f}")
    target.progress(total_used_pct / 100.0, text=f"{total_used_pct:.0f}%")

    target.markdown(f"Profit target {profit_target_pct:.1f}% "
                      f"— ${profit:+,.0f} of ${profit_target_dollars:,.0f}")
    target.progress(profit_pct / 100.0, text=f"{profit_pct:.0f}%")

    if days_required > 0:
        target.markdown(f"Days traded: {days_traded} / {days_required}")
        target.progress(min(1.0, days_traded / days_required))
