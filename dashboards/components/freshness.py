"""
freshness.py — data-staleness widget.

Used by Page 6 (Data Manager) and the global sidebar.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st


def freshness_summary(data_index: dict[str, dict[str, Path]]
                       ) -> tuple[list[dict], int, int, int]:
    """Compute per-file age + bucket counts. Buckets: green<2d, yellow<7d, red>=7d."""
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for ticker, tfs in sorted(data_index.items()):
        for tf, path in sorted(tfs.items()):
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            age_days = (now - mtime).total_seconds() / 86400.0
            if age_days < 2:
                bucket = "green"
            elif age_days < 7:
                bucket = "yellow"
            else:
                bucket = "red"
            rows.append({
                "ticker": ticker, "tf": tf,
                "modified_utc": mtime.isoformat(timespec="seconds"),
                "age_days": round(age_days, 2), "bucket": bucket,
            })
    n_green = sum(1 for r in rows if r["bucket"] == "green")
    n_yellow = sum(1 for r in rows if r["bucket"] == "yellow")
    n_red = sum(1 for r in rows if r["bucket"] == "red")
    return rows, n_green, n_yellow, n_red


def render_freshness_bar(data_index: dict[str, dict[str, Path]]) -> None:
    """Top-of-page status row with bucket counters."""
    if not data_index:
        st.warning("No cached parquets — use Data Manager to fetch some.")
        return
    rows, ng, ny, nr = freshness_summary(data_index)

    cols = st.columns([1, 1, 1, 2])
    cols[0].markdown(
        f"<span style='background:#1c8a4a;color:white;padding:4px 10px;"
        f"border-radius:4px;'>🟢  fresh &lt;2d: <b>{ng}</b></span>",
        unsafe_allow_html=True,
    )
    cols[1].markdown(
        f"<span style='background:#b08800;color:white;padding:4px 10px;"
        f"border-radius:4px;'>🟡  2–7d: <b>{ny}</b></span>",
        unsafe_allow_html=True,
    )
    cols[2].markdown(
        f"<span style='background:#aa1a1a;color:white;padding:4px 10px;"
        f"border-radius:4px;'>🔴  stale &gt;7d: <b>{nr}</b></span>",
        unsafe_allow_html=True,
    )
    if nr > 0:
        cols[3].markdown(
            "🔴 **Some data is older than a week.** Run `make refresh-data` "
            "before trusting backtest results."
        )
    elif ny > 0:
        cols[3].markdown("🟡 Some data 2-7d old; refresh recommended.")
    else:
        cols[3].markdown("🟢 All cached data is current (<2 days).")

    with st.expander("Per-file ages", expanded=False):
        df = pd.DataFrame(rows)
        emoji_map = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
        df["age"] = df["bucket"].map(emoji_map) + " " + df["age_days"].astype(str) + "d"
        st.dataframe(df[["ticker", "tf", "modified_utc", "age"]],
                      width="stretch",
                      height=min(360, 36 * (len(rows) + 1)))
