"""
charts.py — Plotly figure helpers shared across pages.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd
import plotly.graph_objects as go


def equity_figure(eq: pd.DataFrame, split_time: pd.Timestamp,
                   starting_balance: float, title: str) -> go.Figure:
    """Equity curve with optional train/test split marker."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["time"], y=eq["equity"],
        mode="lines", name="equity",
        line=dict(color="#0a4", width=1.4),
    ))
    fig.add_hline(y=starting_balance, line=dict(color="#888", dash="dash"),
                   annotation_text=f"start ${starting_balance:,.0f}",
                   annotation_position="bottom right")
    if split_time is not None:
        fig.add_shape(type="line",
                       x0=split_time, x1=split_time, xref="x",
                       y0=0, y1=1, yref="paper",
                       line=dict(color="#c33", dash="dot"))
        fig.add_annotation(
            x=split_time, y=1.0, xref="x", yref="paper",
            text="train / test split", showarrow=False,
            xanchor="left", yanchor="top",
            font=dict(color="#c33", size=10),
        )
    fig.update_layout(
        title=title, height=360, margin=dict(l=10, r=10, t=40, b=10),
        xaxis_title="time", yaxis_title="equity ($)",
        hovermode="x unified",
    )
    return fig


def drawdown_figure(eq: pd.DataFrame) -> go.Figure:
    if eq.empty:
        return go.Figure()
    peak = eq["equity"].cummax()
    dd_pct = (eq["equity"] - peak) / peak * 100.0
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=eq["time"], y=dd_pct,
        mode="lines", name="drawdown %",
        line=dict(color="#c33", width=0.8),
        fill="tozeroy", fillcolor="rgba(204,51,51,0.25)",
    ))
    fig.update_layout(
        height=180, margin=dict(l=10, r=10, t=10, b=10),
        xaxis_title="time", yaxis_title="drawdown %",
        hovermode="x unified",
    )
    return fig


def trade_reasons_figure(trades) -> go.Figure:
    if not trades:
        return go.Figure().update_layout(height=200, title="(no trades)")
    c = Counter(t.close_reason for t in trades)
    keys = list(c.keys())
    vals = [c[k] for k in keys]
    colors = {
        "target": "#0a4", "stop": "#c33",
        "time": "#aa0", "end_of_data": "#888",
        "weekend_flat": "#5a4ec4", "daily_close_flat": "#3a8ac4",
    }
    fig = go.Figure([go.Bar(
        x=keys, y=vals,
        marker_color=[colors.get(k, "#666") for k in keys],
    )])
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10),
                       title="Close reasons",
                       xaxis_title="reason", yaxis_title="trades")
    return fig


def trades_to_dataframe(trades, candles: pd.DataFrame) -> pd.DataFrame:
    """Turn ClosedTrade list into a sortable DataFrame for st.dataframe."""
    rows = []
    for t in trades:
        rows.append({
            "direction":   t.direction,
            "entry_time":  candles["time"].iloc[t.entry_bar_idx],
            "exit_time":   candles["time"].iloc[t.exit_bar_idx],
            "entry":       round(t.entry_price, 5),
            "exit":        round(t.exit_price, 5),
            "stop":        round(t.stop_price, 5),
            "target":      round(t.target_price, 5),
            "lots":        t.lots,
            "pnl_$":       round(t.realized_pnl, 2),
            "R":           round(t.r_multiple, 3),
            "reason":      t.close_reason,
            "bars_held":   t.exit_bar_idx - t.entry_bar_idx,
        })
    return pd.DataFrame(rows)


def heatmap_test_R(df_grid: pd.DataFrame) -> go.Figure:
    """Heatmap of (ticker × strategy) coloured by mean test_R."""
    if df_grid["tf"].nunique() > 1:
        hm = df_grid.groupby(["ticker", "strategy"])["test_R"].mean().reset_index()
    else:
        hm = df_grid[["ticker", "strategy", "test_R"]]
    pivot = hm.pivot(index="ticker", columns="strategy", values="test_R")
    fig = go.Figure(data=go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorscale="RdYlGn", zmid=0,
        colorbar=dict(title="test_R"),
        text=[[f"{v:+.2f}" if pd.notna(v) else "" for v in row]
               for row in pivot.values],
        texttemplate="%{text}", textfont=dict(size=10),
    ))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=30, b=10),
                       title="test_R heatmap (out-of-sample R per trade)")
    return fig
