"""
theme.py — single inject_css() call any page can use to get a consistent,
Jane-Street-y look: tabular numerals, denser metric boxes, sticky header
on Operations, and a green/red palette tuned for trading P&L.

Idempotent — calling it twice is a no-op. We track that via a flag in
session_state.
"""
from __future__ import annotations

import streamlit as st


_SS_FLAG = "_v2_theme_injected"


CSS = """
<style>
:root {
  --v2-bg-card:    #0f1419;
  --v2-bg-soft:    #0b1117;
  --v2-border:     #1f2937;
  --v2-fg:         #e5e7eb;
  --v2-fg-mute:    #9ca3af;
  --v2-fg-dim:     #6b7280;
  --v2-pos:        #16a34a;
  --v2-neg:        #dc2626;
  --v2-warn:       #f59e0b;
  --v2-accent:     #38bdf8;
}

/* Force tabular numerals in metric values, headers, and any code-styled
   span — keeps columns aligned when balances move. */
[data-testid="stMetricValue"],
[data-testid="stMetricDelta"],
.stMarkdown code,
[data-testid="stMetric"] {
  font-variant-numeric: tabular-nums !important;
  font-feature-settings: "tnum" 1 !important;
}

/* Tighter metric box — cuts wasted whitespace on the dense KPI rows. */
[data-testid="stMetric"] {
  padding: 6px 10px;
}

/* Tab labels with a touch more weight so the sub-pages read clearly. */
[data-testid="stTabs"] button p {
  font-weight: 600;
  font-size: 0.92rem;
}

/* Container/card border slightly cooler so cards don't overpower KPIs. */
div[data-testid="stVerticalBlockBorderWrapper"] {
  border-color: var(--v2-border) !important;
}

/* Buttons: keep the type='primary' green for Go Live (instead of the
   default red Streamlit picks in dark mode). The selector targets only
   primary buttons so secondary ones keep their muted look. */
.stButton button[kind="primary"] {
  background: linear-gradient(180deg, #16a34a 0%, #15803d 100%) !important;
  border-color: #15803d !important;
  color: white !important;
}
.stButton button[kind="primary"]:hover {
  background: linear-gradient(180deg, #22c55e 0%, #16a34a 100%) !important;
  border-color: #16a34a !important;
}
.stButton button[kind="primary"]:disabled {
  background: #1f2937 !important;
  border-color: #374151 !important;
  color: #6b7280 !important;
}

/* Compact header for our custom KPI tiles so they don't overflow. */
.v2-kpi {
  background: var(--v2-bg-card);
  border: 1px solid var(--v2-border);
  border-radius: 8px;
  padding: 10px 14px;
  height: 78px;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

/* Sidebar nav — collapse the page numbers so it just shows the icon + name. */
[data-testid="stSidebarNav"] li a span:first-child {
  letter-spacing: 0.02em;
}
</style>
"""


def inject_css() -> None:
    if st.session_state.get(_SS_FLAG):
        return
    st.markdown(CSS, unsafe_allow_html=True)
    st.session_state[_SS_FLAG] = True
