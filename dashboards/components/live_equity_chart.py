"""
live_equity_chart.py — equity / balance / theoretical / per-strategy stacked.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


def render(*, daily_curve: pd.DataFrame, baseline_equity: float,
            container=None) -> None:
    target = container or st
    target.markdown("### 📈  Equity history")
    if daily_curve is None or daily_curve.empty:
        target.caption("(no broker history yet)")
        return
    df = daily_curve.sort_values("day_start_utc").copy()
    df["cum_pnl"] = df["realized_pnl"].cumsum()
    df["balance"] = baseline_equity + df["cum_pnl"]
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["day_start_utc"], y=df["balance"],
        mode="lines+markers", name="balance",
        line=dict(color="#0c8a3a", width=2),
    ))
    fig.add_hline(y=baseline_equity, line=dict(color="#888", dash="dash"),
                   annotation_text=f"baseline ${baseline_equity:,.0f}")
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=20, b=10),
        xaxis_title="day (FTMO 22:00 UTC boundary)",
        yaxis_title="balance ($)", hovermode="x unified",
    )
    target.plotly_chart(fig, use_container_width=True)
