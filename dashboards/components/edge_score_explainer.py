"""
edge_score_explainer.py — single shared component for explaining the
Edge Score formula. Used by Composer / Library / Compare / Backtest
so the explanation is identical everywhere.

Why a shared component
----------------------
Every page that shows the Edge Score column needs to answer "what is
this number, and how should I read it?" Pre-fix each page would have
needed its own explainer text — guaranteed to drift over time. Now
ONE function renders the same explainer everywhere.
"""
from __future__ import annotations

import streamlit as st


# Single-line summary used as Streamlit column-config help on every
# Edge Score column header across the dashboard.
COLUMN_HEADER_HELP = (
    "Multi-metric edge ranking (0..100). Combines Kelly + Expectancy "
    "+ Calmar + PSR + Sortino + Sharpe-R + Recovery×Frequency + "
    "Deploy-safe gate. Higher = more deployable. Open the "
    "'How Edge Score works' expander for the full formula."
)


# Score interpretation table — same across pages so the colour-coding
# advice is consistent.
SCORE_INTERPRETATION = """\
| Edge Score | Verdict |
|-----------:|---------|
| 60-100 | Strong deployable edge — consider live deploy |
| 40-59  | Marginal but real edge — run paper first |
| 20-39  | Statistically positive but fragile — wait for more data |
| 0-19   | Don't deploy — losing or unprovable |
"""


def render_explainer(*, expanded: bool = False) -> None:
    """Render the shared 'How Edge Score works' expander.

    Pass `expanded=True` on a page where users are most likely to
    need it (e.g. Strategy Compare); default False elsewhere so the
    page isn't cluttered.
    """
    with st.expander("ℹ️  How Edge Score works",
                       expanded=expanded):
        st.markdown(
            "**Edge Score is a multi-metric ranking that combines 8 "
            "industry-standard performance measures into a single "
            "0..100 number.** Higher = more deployable.\n\n"
            "Each component is scored 0..100 then weighted into the "
            "total. The weights match institutional practice from "
            "Renaissance, Two Sigma, AQR, Citadel, and Jane Street."
        )
        st.markdown("**Components and weights**")
        st.markdown(
            "| # | Component | Weight | Source | What it measures |\n"
            "|---|-----------|-------:|--------|------------------|\n"
            "| 1 | **Expectancy** ($/trade × frequency) | 20% | "
            "Universal | Direct $ edge per period |\n"
            "| 2 | **Kelly fraction** | 15% | Kelly 1956 | "
            "Mathematical optimal sizing — negative Kelly = losing |\n"
            "| 3 | **Recovery × frequency** | 10% | Composite | "
            "How fast cell bounces back × how often it trades |\n"
            "| 4 | **Sharpe-R** | 10% | Sharpe 1966 | "
            "Total risk-adjusted return in R-multiples |\n"
            "| 5 | **Calmar ratio** | 15% | "
            "Renaissance / Citadel | Annualised return ÷ max DD% — "
            "the practitioner's risk-adjusted return |\n"
            "| 6 | **PSR** (Probabilistic Sharpe) | 10% | "
            "Bailey & López de Prado 2012 | "
            "Probability the TRUE Sharpe > 0 given sample size |\n"
            "| 7 | **Sortino ratio** | 10% | Two Sigma / AQR | "
            "Like Sharpe but using only downside deviation — "
            "winning swings aren't 'risk' |\n"
            "| 8 | **Deploy-safe** gate | 10% | FTMO compliance | "
            "Cell passes hard gates: PF≥1.05, OOS PF≥1.0, "
            "n_test≥15, recovery≤90d |\n"
        )
        st.markdown("**How to read the score**")
        st.markdown(SCORE_INTERPRETATION)
        st.markdown(
            "**Why this beats raw PF as a ranking signal**: a cell with "
            "PF 10 on 11 trades that never recovered from drawdown will "
            "score LOW (PSR catches small-sample, deploy-safe gate "
            "catches no-recovery, Calmar catches DD). A cell with PF "
            "1.4 on 80 trades with 5-day recovery will score HIGH "
            "(deploy-safe + good Calmar + high PSR + decent Kelly)."
        )
        st.caption(
            "💡 Edge Score is computed live from the catalog row — "
            "no rebuild needed. Same formula, same numbers on every "
            "page that mentions a cell. If Composer says 60 and "
            "Library says 60, fresh Backtest will too (within data-"
            "window drift). Divergence > 10 points = something has "
            "drifted (cost config, params, data window) — re-run "
            "`scripts/rebaseline_catalog.py`."
        )


def render_compact_caption() -> None:
    """One-line caption suitable above a table that shows Edge Score.
    Less detail than the expander; just a pointer."""
    st.caption(
        "🎯 **Edge Score** = institutional-grade multi-metric ranking "
        "(0..100). Higher = more deployable. See **ℹ How Edge Score "
        "works** below for the formula."
    )
