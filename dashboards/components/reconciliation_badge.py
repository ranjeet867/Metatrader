"""
reconciliation_badge.py — INVARIANT-1 visible at the top of every result page.

Shows the actual $ divergence value, NOT just a green/red badge:
  ✅  Reconciliation: $0.0000 divergence  (well under $0.01 tolerance)
  ⛔  RECONCILIATION FAILED  divergence = $1,247.32  > tolerance $0.01
"""
from __future__ import annotations

import streamlit as st


def render(reconciles: bool, sum_pnl: float, eq_pnl: float,
           tolerance: float = 0.01) -> None:
    """Render the reconciliation banner at the top of a result section.

    The exact $ divergence is ALWAYS shown — no green-only "looks good" hiding.
    """
    div = sum_pnl - eq_pnl
    if reconciles:
        st.success(
            f"✅  **Reconciled** — divergence = ${div:+.4f}  "
            f"&nbsp;&nbsp; sum(realized) = ${sum_pnl:+,.2f}  "
            f"&nbsp;&nbsp; equity_pnl = ${eq_pnl:+,.2f}  "
            f"&nbsp;&nbsp; tolerance = ${tolerance}",
        )
    else:
        st.error(
            f"⛔  **RECONCILIATION FAILED**  divergence = ${div:+,.4f}  > tolerance ${tolerance}\n\n"
            f"sum(realized) = ${sum_pnl:+,.2f}  &nbsp;&nbsp;  equity_pnl = ${eq_pnl:+,.2f}\n\n"
            "**Do not trust any number on this page until the divergence is "
            "investigated and fixed.** This blocks paper/live runs of this strategy.",
            icon="⛔",
        )
