"""
6_💾_Data_Manager.py — per-file freshness, refresh, add ticker, gap analysis,
bridge latency chart.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import storage   # noqa: E402
from core.data import fetch_from_bridge, load_parquet, save_parquet   # noqa: E402
from dashboards.components.freshness import (   # noqa: E402
    freshness_summary,
    render_freshness_bar,
)
from dashboards.components.state import discover_data   # noqa: E402


def render_per_file_table(data_index):
    rows, _, _, _ = freshness_summary(data_index)
    if not rows:
        st.info("No cached parquets.")
        return
    st.markdown("**Per-file ages**")
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
    for r in rows:
        cols = st.columns([2, 1, 1, 2, 1])
        cols[0].markdown(f"`{r['ticker']}` `{r['tf']}`")
        cols[1].markdown(f"{emoji[r['bucket']]} {r['age_days']}d")
        cols[2].markdown(r["modified_utc"][:19])
        cols[3].caption(f"path: data/{r['ticker']}_{r['tf']}.parquet")
        if cols[4].button("⬇ Refresh", key=f"rfr_{r['ticker']}_{r['tf']}"):
            _refresh_one(r["ticker"], r["tf"])


def _refresh_one(ticker: str, tf: str, n_bars: int | None = None) -> None:
    if n_bars is None:
        n_bars = 8000 if tf in ("M15", "H1") else 2000
    out_path = REPO / "data" / f"{ticker}_{tf}.parquet"
    prog = st.progress(0.0, text=f"Fetching {ticker} {tf}...")
    try:
        df = fetch_from_bridge(ticker, tf, n_bars)
        prog.progress(0.7, text=f"Validating {len(df)} bars...")
        save_parquet(df, out_path)
        prog.progress(1.0)
        st.success(f"✓ Refreshed {ticker} {tf}: {len(df):,} bars; "
                    f"latest = {df['time'].iloc[-1]}")
    except TimeoutError as e:
        st.error(f"Bridge timeout: {e}")
    except Exception as e:
        st.error(f"Fetch failed: {e}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


def render_add_ticker_form():
    st.markdown("---")
    st.markdown("**Add a new ticker**")
    cols = st.columns([2, 1, 1, 1])
    ticker = cols[0].text_input("symbol (broker-exact)", value="",
                                  key="add_ticker_name")
    tf = cols[1].selectbox("tf", ["M15", "H1", "H4", "D1"], index=1,
                              key="add_ticker_tf")
    n = int(cols[2].number_input("bars", value=8000, step=500,
                                    min_value=200, max_value=100_000,
                                    key="add_ticker_n"))
    if cols[3].button("⬇  Fetch", type="primary", key="add_ticker_btn"):
        if not ticker.strip():
            st.warning("Enter a symbol.")
        else:
            _refresh_one(ticker.strip(), tf, n_bars=n)


def render_gap_analysis(data_index):
    """For each parquet, compute gaps in the bar timeline."""
    st.markdown("---")
    st.markdown("**Gap analysis**")
    rows = []
    for ticker, tfs in sorted(data_index.items()):
        for tf, path in sorted(tfs.items()):
            try:
                df = load_parquet(path)
            except Exception as e:
                rows.append({"ticker": ticker, "tf": tf,
                             "n_bars": 0, "n_gaps": "ERROR",
                             "first": "", "last": "", "note": str(e)})
                continue
            n = len(df)
            if n < 2:
                rows.append({"ticker": ticker, "tf": tf, "n_bars": n,
                             "n_gaps": 0, "first": "", "last": "", "note": ""})
                continue
            diffs = df["time"].diff().dt.total_seconds().dropna()
            median_s = diffs.median()
            # A gap is a diff > 1.5x median (allows for weekends without flagging)
            if tf in ("M15", "H1", "H4"):
                threshold = median_s * 5    # allow weekends as larger gaps
            else:
                threshold = median_s * 4
            n_gaps = int((diffs > threshold).sum())
            rows.append({
                "ticker": ticker, "tf": tf, "n_bars": n,
                "n_gaps": n_gaps,
                "first": str(df["time"].iloc[0])[:19],
                "last": str(df["time"].iloc[-1])[:19],
                "note": "" if n_gaps == 0 else f"⚠️ {n_gaps} long-gaps",
            })
    df_g = pd.DataFrame(rows)
    st.dataframe(df_g, use_container_width=True, height=min(400, 36 * (len(df_g) + 1)))


def render_bridge_latency_chart(db_path: Path):
    st.markdown("---")
    st.markdown("**Bridge latency (last 100 pings)**")
    try:
        with storage.connect(db_path) as c:
            rows = c.execute(
                "SELECT pinged_at_utc, latency_ms, ok, method "
                "FROM bridge_events ORDER BY ROWID DESC LIMIT 100"
            ).fetchall()
    except Exception as e:
        st.caption(f"(no bridge_events yet: {e})")
        return
    if not rows:
        st.caption("(no bridge events recorded yet — refresh some data first)")
        return
    df = pd.DataFrame(rows, columns=["time", "latency_ms", "ok", "method"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time")
    fig = go.Figure()
    ok_df = df[df["ok"] == 1]
    fail_df = df[df["ok"] == 0]
    fig.add_trace(go.Scatter(
        x=ok_df["time"], y=ok_df["latency_ms"], mode="markers+lines",
        name="ok", marker=dict(color="#0a4", size=6),
        line=dict(color="#0a4", width=1),
    ))
    if not fail_df.empty:
        fig.add_trace(go.Scatter(
            x=fail_df["time"], y=fail_df["latency_ms"], mode="markers",
            name="fail", marker=dict(color="#c33", size=10, symbol="x"),
        ))
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10),
                       xaxis_title="time", yaxis_title="latency (ms)")
    st.plotly_chart(fig, use_container_width=True)


def main():
    st.set_page_config(page_title="Data Manager", page_icon="💾", layout="wide")
    st.title("💾  Data Manager")
    data_index = discover_data()
    render_freshness_bar(data_index)
    st.markdown("---")
    render_per_file_table(data_index)
    render_add_ticker_form()
    render_gap_analysis(data_index)
    render_bridge_latency_chart(REPO / "data" / "v2.db")


main()
