"""
app.py — multi-page entry for the v2 trading dashboard.

Streamlit's pages/ convention: any `pages/*.py` file is auto-discovered as
a navigable page in the order of its filename prefix. This file is the
LANDING page (a quick health overview + sidebar).

Run with:  make dashboard   →  streamlit run dashboards/app.py --server.port 8502
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.config import load_config   # noqa: E402
from core.time_guards import time_guard_cfg_from_risk_config   # noqa: E402
from dashboards.components.freshness import render_freshness_bar   # noqa: E402
from dashboards.components.mt5_status import (   # noqa: E402
    render_bridge_dot,
    render_emergency_stop_indicator,
)
from dashboards.components.state import discover_data, discover_strategies   # noqa: E402
from dashboards.components.time_guard_status import render_countdowns   # noqa: E402


def render_sidebar(cfg) -> None:
    """The global sidebar — visible on every page."""
    st.sidebar.markdown("### 📈  v2 Quant Trader")
    st.sidebar.caption("FTMO-aware, reconciliation-enforced.")
    st.sidebar.markdown("---")

    # Bridge dot (TODO: query latest bridge_events for real ping)
    render_bridge_dot(st.sidebar)

    # Emergency stop visible everywhere
    render_emergency_stop_indicator(REPO / "data", container=st.sidebar)

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 🕒  Time guards")
    tg_cfg = time_guard_cfg_from_risk_config(cfg)
    render_countdowns(tg_cfg, container=st.sidebar)

    st.sidebar.markdown("---")
    st.sidebar.caption(
        f"UTC now: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}`"
    )
    st.sidebar.caption(
        "Reconciliation tolerance: $0.01 — every result page shows the "
        "actual divergence value, not just a green dot."
    )


def main() -> None:
    st.set_page_config(page_title="v2 Quant Trader", layout="wide",
                        initial_sidebar_state="expanded",
                        page_icon="📈")
    cfg = load_config()
    render_sidebar(cfg)

    st.title("📈  v2 Quant Trader — multi-page dashboard")
    st.caption(
        "Every result on this site goes through `core.backtest.compute_realized_pnl`. "
        "If you ever see a ⛔ red banner, do NOT trust the numbers on that page."
    )

    # Quick navigation hints — Streamlit auto-renders the page list in the
    # sidebar from /pages/, but a one-line summary on the landing page is
    # nice for a first-time visitor.
    st.subheader("Pages")
    st.markdown(
        """
- **📊 Backtest** — pick a strategy + ticker + tf, run a single backtest with
  reconciliation enforced. Time-guard toggles available. Promote winners to
  Strategy Studio or Paper Portfolio.
- **🔬 Strategy Studio** — two-column comparison, single-param sweep, walk-forward.
- **📡 Paper / Live** — replay an OOS slice (animated equity), spawn paper
  loops, or — once all 7 pre-flight gates are green — go live.
- **📈 Performance** — combined journal across backtest / paper / live;
  R-distribution, drawdown, monthly heatmap, editable trade notes.
- **⚙️ Account & Risk** — live MT5 account info; FTMO progress; risk caps;
  EMERGENCY STOP. Time-guard countdowns; FTMO pass-rate simulator.
- **💾 Data Manager** — per-file freshness, refresh from MT5 bridge,
  add new ticker, gap analysis.
"""
    )

    st.subheader("Data freshness")
    render_freshness_bar(discover_data())

    st.subheader("Strategy inventory")
    strats = discover_strategies()
    cols = st.columns(4)
    for i, name in enumerate(sorted(strats.keys())):
        cols[i % 4].markdown(f"- `{name}`")
    st.caption(f"{len(strats)} strategies discovered. "
                "All replay-parity tested before going live.")


if __name__ == "__main__":
    main()
