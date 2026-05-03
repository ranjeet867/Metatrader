"""
6_💾_Data_Manager.py — fetch parquets, see freshness, add tickers, scan
for gaps. The page is built around the question "is my data current
enough to trust today's signals?" with one big Fetch-All button up top.
"""
from __future__ import annotations

import sys
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
from dashboards.components import theme   # noqa: E402


def _bars_for_tf(tf: str) -> int:
    return {"M15": 8000, "H1": 8000, "H4": 4000, "D1": 2000}.get(tf, 8000)


def _refresh_one(ticker: str, tf: str, n_bars: int | None = None,
                  *, _silent: bool = False) -> bool:
    if n_bars is None:
        n_bars = _bars_for_tf(tf)
    out_path = REPO / "data" / f"{ticker}_{tf}.parquet"
    try:
        df = fetch_from_bridge(ticker, tf, n_bars)
        save_parquet(df, out_path)
        if not _silent:
            st.toast(f"✓ {ticker} {tf}: {len(df):,} bars",
                       icon="✅")
        return True
    except TimeoutError as e:
        if not _silent:
            st.error(f"Bridge timeout for {ticker} {tf}: {e}")
        return False
    except Exception as e:
        if not _silent:
            st.error(f"Fetch failed for {ticker} {tf}: {e}")
        return False


def _fetch_all(data_index) -> tuple[int, int]:
    """Refresh every (ticker, tf) cell. Returns (n_ok, n_total)."""
    total = sum(len(tfs) for tfs in data_index.values())
    if total == 0:
        return 0, 0
    prog = st.progress(0.0, text="Starting batch refresh…")
    done = 0
    ok_count = 0
    for ticker, tfs in sorted(data_index.items()):
        for tf in sorted(tfs.keys()):
            done += 1
            prog.progress(done / total,
                            text=f"Fetching {ticker} {tf} "
                                  f"({done}/{total})…")
            if _refresh_one(ticker, tf, _silent=True):
                ok_count += 1
    prog.empty()
    return ok_count, total


# ---------------------------------------------------------------------------
# Top action band
# ---------------------------------------------------------------------------

def render_top_band(data_index) -> None:
    rows, n_green, n_yellow, n_red = freshness_summary(data_index)
    n_total = len(rows)

    cols = st.columns([2, 1, 1, 1, 1])
    cols[0].markdown("### 📡  Data freshness")
    cols[1].metric("🟢 fresh", n_green)
    cols[2].metric("🟡 aging", n_yellow)
    cols[3].metric("🔴 stale", n_red)
    if cols[4].button("🔄  Fetch ALL", type="primary",
                       use_container_width=True,
                       help=f"Refresh all {n_total} parquets from the MT5 "
                            "bridge in one shot."):
        n_ok, n_tot = _fetch_all(data_index)
        if n_ok == n_tot:
            st.success(f"✓ Refreshed all {n_tot} parquets from MT5 bridge.")
        else:
            st.warning(
                f"Refreshed {n_ok}/{n_tot}. Some failed — "
                f"see per-row errors below.")
        st.rerun()


# ---------------------------------------------------------------------------
# Per-ticker grid
# ---------------------------------------------------------------------------

def render_per_ticker_grid(data_index) -> None:
    rows, _, _, _ = freshness_summary(data_index)
    if not rows:
        st.info("No cached parquets yet — use **Add a new ticker** below.")
        return
    # Group by ticker
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    st.markdown("### Per-ticker data")

    bucket_color = {"green": "#16a34a", "yellow": "#f59e0b", "red": "#dc2626"}
    for ticker in sorted(by_ticker.keys()):
        with st.container(border=True):
            head = st.columns([3, 1])
            head[0].markdown(f"#### `{ticker}`")
            if head[1].button("⬇ Refresh all TFs",
                                key=f"rfr_all_{ticker}",
                                use_container_width=True):
                with st.spinner(f"Refreshing {ticker}…"):
                    for r in by_ticker[ticker]:
                        _refresh_one(r["ticker"], r["tf"], _silent=True)
                st.toast(f"Refreshed all timeframes for {ticker}",
                          icon="✅")
                st.rerun()
            # Per-TF row
            for r in sorted(by_ticker[ticker], key=lambda x: x["tf"]):
                color = bucket_color.get(r["bucket"], "#6b7280")
                row = st.columns([1, 1, 2, 2, 1])
                row[0].markdown(
                    f"<span style='color:{color};font-weight:600;"
                    f"font-family:ui-monospace,Menlo,monospace;"
                    f"font-size:0.95rem;'>● {r['tf']}</span>",
                    unsafe_allow_html=True,
                )
                row[1].markdown(
                    f"<span style='color:#9ca3af;font-size:0.8rem;"
                    f"font-family:ui-monospace,Menlo,monospace;'>"
                    f"{r['age_days']}d old</span>",
                    unsafe_allow_html=True,
                )
                row[2].markdown(
                    f"<span style='color:#9ca3af;font-size:0.8rem;'>"
                    f"last bar: {r['modified_utc'][:19]}</span>",
                    unsafe_allow_html=True,
                )
                row[3].markdown(
                    f"<span style='color:#6b7280;font-size:0.78rem;"
                    f"font-family:ui-monospace,Menlo,monospace;'>"
                    f"data/{r['ticker']}_{r['tf']}.parquet</span>",
                    unsafe_allow_html=True,
                )
                if row[4].button("⬇", key=f"rfr_{r['ticker']}_{r['tf']}",
                                    use_container_width=True,
                                    help=f"Refresh {r['ticker']} {r['tf']}"):
                    with st.spinner(f"Fetching {r['ticker']} {r['tf']}…"):
                        _refresh_one(r["ticker"], r["tf"])
                    st.rerun()


# ---------------------------------------------------------------------------
# Add a new ticker
# ---------------------------------------------------------------------------

def render_add_ticker_form() -> None:
    with st.expander("➕  Add a new ticker", expanded=False):
        with st.form("add_ticker_form"):
            cols = st.columns([3, 1, 1])
            ticker = cols[0].text_input(
                "symbol (broker-exact)", value="",
                placeholder="e.g. XAUUSD or NDX.cash",
                key="add_ticker_name",
            )
            tf = cols[1].selectbox("tf", ["M15", "H1", "H4", "D1"],
                                      index=1, key="add_ticker_tf")
            n = int(cols[2].number_input("bars", value=8000, step=500,
                                            min_value=200, max_value=100_000,
                                            key="add_ticker_n"))
            submitted = st.form_submit_button("⬇  Fetch", type="primary")
            if submitted:
                if not ticker.strip():
                    st.warning("Enter a symbol.")
                else:
                    with st.spinner(f"Fetching {ticker.strip()} {tf}…"):
                        _refresh_one(ticker.strip(), tf, n_bars=n)
                    st.rerun()


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------

def render_gap_analysis(data_index) -> None:
    with st.expander("🔍  Gap analysis", expanded=False):
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
                                  "n_gaps": 0, "first": "", "last": "",
                                  "note": ""})
                    continue
                diffs = df["time"].diff().dt.total_seconds().dropna()
                median_s = diffs.median()
                threshold = (median_s * 5 if tf in ("M15", "H1", "H4")
                              else median_s * 4)
                n_gaps = int((diffs > threshold).sum())
                rows.append({
                    "ticker": ticker, "tf": tf, "n_bars": n,
                    "n_gaps": n_gaps,
                    "first": str(df["time"].iloc[0])[:19],
                    "last": str(df["time"].iloc[-1])[:19],
                    "note": "" if n_gaps == 0 else f"⚠️ {n_gaps} long-gaps",
                })
        df_g = pd.DataFrame(rows)
        st.dataframe(df_g, use_container_width=True,
                       height=min(400, 36 * (len(df_g) + 1)))


# ---------------------------------------------------------------------------
# Bridge latency
# ---------------------------------------------------------------------------

def render_bridge_latency_chart(db_path: Path) -> None:
    with st.expander("📡  Bridge latency (last 100 pings)", expanded=False):
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
            st.caption(
                "(no bridge events recorded yet — refresh some data first)")
            return
        df = pd.DataFrame(rows,
                            columns=["time", "latency_ms", "ok", "method"])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.sort_values("time")
        fig = go.Figure()
        ok_df = df[df["ok"] == 1]
        fail_df = df[df["ok"] == 0]
        fig.add_trace(go.Scatter(
            x=ok_df["time"], y=ok_df["latency_ms"], mode="markers+lines",
            name="ok", marker=dict(color="#16a34a", size=6),
            line=dict(color="#16a34a", width=1),
        ))
        if not fail_df.empty:
            fig.add_trace(go.Scatter(
                x=fail_df["time"], y=fail_df["latency_ms"], mode="markers",
                name="fail",
                marker=dict(color="#dc2626", size=10, symbol="x"),
            ))
        fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10),
                            xaxis_title="time", yaxis_title="latency (ms)",
                            plot_bgcolor="#0b1117",
                            paper_bgcolor="#0b1117",
                            font=dict(color="#cbd5e1"))
        st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Data Manager", page_icon="💾",
                        layout="wide")
    theme.inject_css()
    st.title("💾  Data Manager")
    data_index = discover_data()
    render_top_band(data_index)
    render_freshness_bar(data_index)
    st.markdown("")
    render_per_ticker_grid(data_index)
    render_add_ticker_form()
    render_gap_analysis(data_index)
    render_bridge_latency_chart(REPO / "data" / "v2.db")


main()
