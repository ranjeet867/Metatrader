"""
4_📈_Performance.py — combined trade journal across backtest / paper / live
+ R-distribution / drawdown / monthly heatmap / editable notes.

Pulls trades from data/v2.db (mode in {backtest, paper, live}). Filters by
time range, mode, strategy, ticker, close_reason.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import storage   # noqa: E402


@st.cache_data(ttl=10)
def _load_trades(db_path: str) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame()
    with storage.connect(db_path) as c:
        rows = c.execute("""
            SELECT t.run_id, t.trade_idx, t.symbol, t.direction,
                   t.opened_at_utc, t.closed_at_utc,
                   t.entry_price, t.exit_price, t.lots,
                   t.realized_pnl, t.r_multiple, t.close_reason,
                   t.mode, t.strategy, t.tf, n.note
            FROM trades t
            LEFT JOIN trade_notes n ON n.trade_run_id = t.run_id
                                    AND n.trade_idx = t.trade_idx
            WHERE t.realized_pnl IS NOT NULL
            ORDER BY t.closed_at_utc
        """).fetchall()
    cols = ["run_id", "trade_idx", "symbol", "direction",
             "opened_at_utc", "closed_at_utc", "entry", "exit", "lots",
             "pnl", "R", "close_reason", "mode", "strategy", "tf", "note"]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["closed_at_utc"] = pd.to_datetime(df["closed_at_utc"], utc=True,
                                                errors="coerce")
        df["opened_at_utc"] = pd.to_datetime(df["opened_at_utc"], utc=True,
                                                errors="coerce")
    return df


def render_filters(df: pd.DataFrame) -> pd.DataFrame:
    st.sidebar.markdown("### 🔎  Filters")
    if df.empty:
        return df
    modes = sorted(df["mode"].dropna().unique().tolist())
    sel_modes = st.sidebar.multiselect("mode", modes, default=modes,
                                          key="perf_modes")
    strats = sorted(df["strategy"].dropna().unique().tolist())
    sel_strats = st.sidebar.multiselect("strategy", strats, default=strats,
                                           key="perf_strats")
    syms = sorted(df["symbol"].dropna().unique().tolist())
    sel_syms = st.sidebar.multiselect("symbol", syms, default=syms,
                                         key="perf_syms")
    reasons = sorted(df["close_reason"].dropna().unique().tolist())
    sel_reasons = st.sidebar.multiselect("close_reason", reasons,
                                            default=reasons, key="perf_reasons")
    f = df.copy()
    if sel_modes:
        f = f[f["mode"].isin(sel_modes)]
    if sel_strats:
        f = f[f["strategy"].isin(sel_strats)]
    if sel_syms:
        f = f[f["symbol"].isin(sel_syms)]
    if sel_reasons:
        f = f[f["close_reason"].isin(sel_reasons)]
    return f.reset_index(drop=True)


def render_metrics(df: pd.DataFrame):
    n = len(df)
    if n == 0:
        st.info("No trades match these filters.")
        return
    pnl = float(df["pnl"].sum())
    wins = (df["pnl"] > 0).sum()
    win_rate = wins / n * 100
    gw = df.loc[df["pnl"] > 0, "pnl"].sum()
    gl = -df.loc[df["pnl"] <= 0, "pnl"].sum()
    pf = gw / gl if gl > 0 else float("inf") if gw > 0 else 0
    avg_R = df["R"].mean()
    cols = st.columns(5)
    cols[0].metric("trades", n)
    cols[1].metric("win rate", f"{win_rate:.1f}%")
    pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
    cols[2].metric("profit factor", pf_text)
    cols[3].metric("avg R", f"{avg_R:+.3f}")
    cols[4].metric("net P&L", f"${pnl:+,.2f}")


def render_equity(df: pd.DataFrame):
    if df.empty:
        return
    df_s = df.sort_values("closed_at_utc").copy()
    df_s["cum_pnl"] = df_s["pnl"].cumsum()
    fig = go.Figure(go.Scatter(
        x=df_s["closed_at_utc"], y=df_s["cum_pnl"],
        mode="lines", line=dict(color="#0a4", width=1.5),
    ))
    fig.update_layout(title="Cumulative P&L (filtered)",
                       xaxis_title="time", yaxis_title="$",
                       height=320, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)
    # Drawdown
    peak = df_s["cum_pnl"].cummax()
    dd = df_s["cum_pnl"] - peak
    fig2 = go.Figure(go.Scatter(
        x=df_s["closed_at_utc"], y=dd, mode="lines",
        line=dict(color="#c33", width=0.8), fill="tozeroy",
        fillcolor="rgba(204,51,51,0.25)",
    ))
    fig2.update_layout(title="Drawdown ($)",
                        xaxis_title="time", yaxis_title="$",
                        height=180, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig2, use_container_width=True)


def render_r_distribution(df: pd.DataFrame):
    if df.empty:
        return
    fig = go.Figure(go.Histogram(
        x=df["R"], nbinsx=40, marker_color="#3a8ac4",
    ))
    fig.update_layout(title="R-multiple distribution",
                       xaxis_title="R", yaxis_title="trades",
                       height=300, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_monthly_heatmap(df: pd.DataFrame):
    if df.empty or df["closed_at_utc"].isna().all():
        return
    df = df.copy()
    df["year"] = df["closed_at_utc"].dt.year
    df["month"] = df["closed_at_utc"].dt.month
    monthly = df.groupby(["year", "month"])["pnl"].sum().reset_index()
    pivot = monthly.pivot(index="year", columns="month", values="pnl")
    if pivot.empty:
        return
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorscale="RdYlGn", zmid=0,
        text=[[f"${v:+,.0f}" if pd.notna(v) else "" for v in row]
               for row in pivot.values],
        texttemplate="%{text}", textfont=dict(size=10),
        colorbar=dict(title="$"),
    ))
    fig.update_layout(title="Monthly P&L heatmap (year × month)",
                       xaxis_title="month", yaxis_title="year",
                       height=320, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_attribution_pie(df: pd.DataFrame):
    if df.empty:
        return
    by_strat = df.groupby("strategy")["pnl"].sum().sort_values(ascending=False)
    fig = go.Figure(go.Pie(
        labels=by_strat.index.tolist(),
        values=by_strat.values.tolist(),
        hole=0.45,
    ))
    fig.update_layout(title="P&L attribution by strategy",
                       height=320, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_journal_table(df: pd.DataFrame, db_path: str):
    if df.empty:
        return
    st.markdown("---")
    st.markdown("**Trade journal** — edit `note` cells; press Enter to save.")
    edited = st.data_editor(
        df[["closed_at_utc", "mode", "strategy", "symbol", "tf",
            "direction", "entry", "exit", "lots", "pnl", "R",
            "close_reason", "note", "run_id", "trade_idx"]],
        use_container_width=True, height=480,
        disabled=["closed_at_utc", "mode", "strategy", "symbol", "tf",
                  "direction", "entry", "exit", "lots", "pnl", "R",
                  "close_reason", "run_id", "trade_idx"],
        key="perf_journal_editor",
    )
    # Persist any note changes
    changed = (edited["note"].fillna("") != df["note"].fillna(""))
    if changed.any():
        now = datetime.now(timezone.utc).isoformat()
        for _, row in edited[changed].iterrows():
            storage.upsert_trade_note(db_path, row["run_id"], row["trade_idx"],
                                       row["note"] or "", now)
        st.toast(f"Saved {changed.sum()} note(s)", icon="💾")


def main():
    st.set_page_config(page_title="Performance", page_icon="📈", layout="wide")
    st.title("📈  Performance")
    db_path = str(REPO / "data" / "v2.db")
    df_all = _load_trades(db_path)
    df = render_filters(df_all)
    render_metrics(df)
    render_equity(df)
    cols = st.columns(2)
    with cols[0]:
        render_r_distribution(df)
    with cols[1]:
        render_attribution_pie(df)
    render_monthly_heatmap(df)
    render_journal_table(df, db_path)


main()
