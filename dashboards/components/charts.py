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
    """Turn ClosedTrade list into a sortable DataFrame for st.dataframe.

    Includes `lots` and `$_at_risk` (= initial_dollar_risk) so the user
    can verify the dynamic-sizing path produced the intended risk per trade.
    """
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
            "lots":        round(t.lots, 4),
            "$_at_risk":   round(t.initial_dollar_risk, 2),
            "pnl_$":       round(t.realized_pnl, 2),
            "R":           round(t.r_multiple, 3),
            "reason":      t.close_reason,
            "bars_held":   t.exit_bar_idx - t.entry_bar_idx,
        })
    return pd.DataFrame(rows)


def win_loss_donut(trades) -> go.Figure:
    """AlgoTest-style donut: wins vs losses vs break-even. With centre
    label showing win rate %."""
    if not trades:
        return go.Figure().update_layout(height=240, title="(no trades)")
    n = len(trades)
    wins = sum(1 for t in trades if t.realized_pnl > 0)
    losses = sum(1 for t in trades if t.realized_pnl < 0)
    even = n - wins - losses
    win_pct = wins / n * 100.0 if n else 0.0
    fig = go.Figure(data=[go.Pie(
        labels=["Wins", "Losses", "Break-even"],
        values=[wins, losses, even],
        hole=0.62,
        marker=dict(colors=["#16a34a", "#dc2626", "#6b7280"]),
        textinfo="label+percent", textposition="outside",
        sort=False,
    )])
    fig.update_layout(
        height=300, margin=dict(l=10, r=10, t=40, b=10),
        title=f"Win / Loss split — {n} trades",
        showlegend=False, plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        annotations=[dict(
            text=f"<b>{win_pct:.1f}%</b><br><span style='font-size:11px'>"
                 f"win rate</span>",
            x=0.5, y=0.5, font_size=22, showarrow=False,
            font_color="#cbd5e1",
        )]
    )
    return fig


def streak_figure(trades) -> go.Figure:
    """Bar chart of consecutive win/loss streaks in chronological order.

    Each bar is one streak — height = streak length, colour = win/loss.
    Hovering shows the date range. AlgoTest's "streak chart".
    """
    if not trades:
        return go.Figure().update_layout(height=240, title="(no trades)")
    streaks: list[tuple[str, int, int]] = []  # (kind, length, end_idx)
    cur_kind = None
    cur_len = 0
    for i, t in enumerate(trades):
        kind = "win" if t.realized_pnl > 0 else (
            "loss" if t.realized_pnl < 0 else "even")
        if kind == cur_kind:
            cur_len += 1
        else:
            if cur_kind is not None:
                streaks.append((cur_kind, cur_len, i - 1))
            cur_kind, cur_len = kind, 1
    if cur_kind is not None:
        streaks.append((cur_kind, cur_len, len(trades) - 1))
    if not streaks:
        return go.Figure().update_layout(height=240, title="(no streaks)")
    xs = list(range(len(streaks)))
    ys = [s[1] if s[0] == "win" else -s[1] for s in streaks]
    colors = [("#16a34a" if s[0] == "win"
                else "#dc2626" if s[0] == "loss"
                else "#6b7280") for s in streaks]
    text = [f"{s[0]}: {s[1]}" for s in streaks]
    fig = go.Figure(go.Bar(
        x=xs, y=ys, marker_color=colors,
        text=text, hovertemplate="%{text}<extra></extra>",
    ))
    max_win = max((s[1] for s in streaks if s[0] == "win"), default=0)
    max_loss = max((s[1] for s in streaks if s[0] == "loss"), default=0)
    fig.update_layout(
        height=260, margin=dict(l=10, r=10, t=40, b=10),
        title=f"Win/Loss streaks — longest win {max_win}, "
              f"longest loss {max_loss}",
        xaxis_title="streak # (chronological)",
        yaxis_title="streak length (+ wins / − losses)",
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937", showticklabels=False),
        yaxis=dict(gridcolor="#1f2937", zeroline=True,
                    zerolinecolor="#4b5563"),
        showlegend=False,
    )
    return fig


def cumulative_pnl_figure(trades) -> go.Figure:
    """Per-trade cumulative P&L curve. Each step = one trade. Shows
    AlgoTest-style 'growth' independent of bar timestamps."""
    if not trades:
        return go.Figure().update_layout(height=240, title="(no trades)")
    cum = 0.0
    xs, ys = [0], [0.0]
    peak = 0.0
    for i, t in enumerate(trades, 1):
        cum += t.realized_pnl
        peak = max(peak, cum)
        xs.append(i)
        ys.append(cum)
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines",
        name="cumulative P&L",
        line=dict(color="#38bdf8", width=2),
        fill="tozeroy", fillcolor="rgba(56,189,248,0.10)",
    ))
    fig.add_hline(y=peak, line=dict(color="#16a34a", dash="dash"),
                   annotation_text=f"peak ${peak:+,.0f}",
                   annotation_position="top right",
                   annotation_font=dict(color="#16a34a", size=10))
    fig.update_layout(
        height=260, margin=dict(l=10, r=10, t=40, b=10),
        title="Cumulative P&L by trade",
        xaxis_title="trade #", yaxis_title="cumulative $",
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937"),
        yaxis=dict(gridcolor="#1f2937", zeroline=True,
                    zerolinecolor="#4b5563"),
        hovermode="x unified",
    )
    return fig


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
