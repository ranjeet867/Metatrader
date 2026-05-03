"""
live_equity_chart.py — broker-truth equity history.

Series:
  • balance (cumulative): baseline + cumulative realized PnL
  • daily PnL bars (positive green, negative red)
  • baseline horizontal reference
  • daily-loss cap and total-loss cap horizontal references (red dashed)
  • profit-target horizontal reference (green dashed)

We draw bars + line on the same axis since balance and per-day PnL are
both in $; a secondary y-axis would just confuse the read.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render(*, daily_curve: pd.DataFrame, baseline_equity: float,
           daily_loss_cap_pct: float = 5.0,
           total_loss_cap_pct: float = 10.0,
           profit_target_pct: float = 10.0,
           container=None) -> None:
    target = container or st
    target.markdown("### 📈  Equity history (broker-truth)")
    if daily_curve is None or daily_curve.empty:
        target.caption("(no broker history yet — once trades close they'll "
                        "appear here)")
        return

    df = daily_curve.sort_values("day_start_utc").copy()
    df["cum_pnl"] = df["realized_pnl"].cumsum()
    df["balance"] = baseline_equity + df["cum_pnl"]

    bar_colors = ["#16a34a" if x >= 0 else "#dc2626" for x in df["realized_pnl"]]

    fig = go.Figure()

    # Daily PnL bars (faint background)
    fig.add_trace(go.Bar(
        x=df["day_start_utc"], y=df["realized_pnl"],
        marker_color=bar_colors, opacity=0.35,
        name="daily realized PnL", yaxis="y2",
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>"
                      "daily PnL: $%{y:,.2f}<extra></extra>",
    ))

    # Cumulative balance line
    fig.add_trace(go.Scatter(
        x=df["day_start_utc"], y=df["balance"],
        mode="lines+markers", name="balance",
        line=dict(color="#38bdf8", width=2.5),
        marker=dict(size=5, color="#38bdf8"),
        hovertemplate="<b>%{x|%Y-%m-%d}</b><br>"
                      "balance: $%{y:,.2f}<extra></extra>",
    ))

    # Reference lines
    fig.add_hline(y=baseline_equity, line=dict(color="#9ca3af", dash="dot",
                                                  width=1),
                   annotation_text=f"baseline ${baseline_equity:,.0f}",
                   annotation_position="top right",
                   annotation_font=dict(color="#9ca3af", size=10))
    daily_floor = baseline_equity * (1 - daily_loss_cap_pct / 100.0)
    fig.add_hline(y=daily_floor, line=dict(color="#dc2626", dash="dash",
                                              width=1),
                   annotation_text=f"daily-loss floor "
                                    f"({daily_loss_cap_pct:.1f}%)",
                   annotation_position="bottom right",
                   annotation_font=dict(color="#dc2626", size=10))
    total_floor = baseline_equity * (1 - total_loss_cap_pct / 100.0)
    fig.add_hline(y=total_floor, line=dict(color="#dc2626", dash="dash",
                                              width=1),
                   annotation_text=f"total-loss floor "
                                    f"({total_loss_cap_pct:.1f}%)",
                   annotation_position="bottom right",
                   annotation_font=dict(color="#dc2626", size=10))
    profit_target = baseline_equity * (1 + profit_target_pct / 100.0)
    fig.add_hline(y=profit_target, line=dict(color="#16a34a", dash="dash",
                                                width=1),
                   annotation_text=f"profit target "
                                    f"({profit_target_pct:.1f}%)",
                   annotation_position="top right",
                   annotation_font=dict(color="#16a34a", size=10))

    fig.update_layout(
        height=360,
        margin=dict(l=10, r=10, t=20, b=10),
        xaxis=dict(title="day (FTMO 22:00 UTC reset)",
                   gridcolor="#1f2937"),
        yaxis=dict(title="balance ($)", gridcolor="#1f2937"),
        yaxis2=dict(overlaying="y", side="right", showgrid=False,
                    title="daily ($)",
                    title_font=dict(color="#9ca3af"),
                    tickfont=dict(color="#9ca3af")),
        hovermode="x unified",
        legend=dict(orientation="h", x=0, y=1.10, bgcolor="rgba(0,0,0,0)"),
        plot_bgcolor="#0b1117",
        paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        bargap=0.4,
    )
    target.plotly_chart(fig, use_container_width=True)
