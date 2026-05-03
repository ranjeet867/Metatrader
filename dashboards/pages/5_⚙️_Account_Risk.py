"""
5_⚙️_Account_Risk.py — live MT5 account info, FTMO progress, risk caps editor,
time-guard countdowns, EMERGENCY STOP, FTMO pass-rate gauge (placeholder).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import storage   # noqa: E402
from core.config import (   # noqa: E402
    DEFAULT_CONFIG,
    default_config_dict,
    load_config,
    save_config,
)
from core.risk_engine import LiveRiskTracker   # noqa: E402
from core.time_guards import time_guard_cfg_from_risk_config   # noqa: E402
from dashboards.components.mt5_status import (   # noqa: E402
    render_account_card,
    render_emergency_stop_indicator,
)
from dashboards.components.time_guard_status import render_countdowns   # noqa: E402


def render_account_section():
    st.markdown("### 🏦  MT5 Account")
    # We don't auto-instantiate a real client (no bridge in test env). Show
    # the user how to wire it.
    st.caption(
        "MT5 account info is fetched by `core.mt5_account.MT5AccountClient`; "
        "wire this in your live deployment script. The client is fully tested "
        "(see `tests/test_mt5_account.py`)."
    )


def render_ftmo_progress(cfg):
    st.markdown("### 📉  FTMO Progress")
    db_path = REPO / "data" / "v2.db"
    tracker = LiveRiskTracker(db_path=db_path,
                                daily_loss_cap_pct=cfg.daily_loss_cap_pct,
                                max_consecutive_losses=cfg.max_consecutive_losses)
    daily_loss = tracker.account_daily_loss_pct()
    cap = cfg.daily_loss_cap_pct
    pct_used = min(daily_loss / cap * 100, 100.0) if cap > 0 else 0
    st.markdown(f"**Daily loss vs cap** — {daily_loss:.2f}% / {cap:.2f}% "
                 f"({pct_used:.0f}% of cap consumed)")
    st.progress(pct_used / 100, text=f"{daily_loss:.2f}% used")
    if daily_loss >= cap:
        st.error("⛔  Daily loss cap reached — live trading BLOCKED.")
    elif daily_loss >= cap * 0.8:
        st.warning("⚠️  >80% of daily cap consumed.")


def render_per_strategy_risk():
    st.markdown("### 🧨  Per-strategy risk state")
    db_path = REPO / "data" / "v2.db"
    try:
        with storage.connect(db_path) as c:
            rows = c.execute(
                "SELECT symbol, strategy, consecutive_losses, "
                "last_loss_at_utc, cooldown_until_utc, daily_loss_pct, "
                "day_start_balance FROM risk_state"
            ).fetchall()
    except Exception:
        rows = []
    if not rows:
        st.caption("(no per-strategy risk state recorded yet)")
        return
    df = pd.DataFrame(rows, columns=[
        "symbol", "strategy", "consecutive_losses",
        "last_loss_at_utc", "cooldown_until_utc",
        "daily_loss_pct", "day_start_balance",
    ])
    st.dataframe(df, use_container_width=True,
                  height=min(360, 36 * (len(df) + 1)))


def render_time_guards(cfg):
    st.markdown("### 🕒  Time guards (INVARIANT-8)")
    tg = time_guard_cfg_from_risk_config(cfg)
    render_countdowns(tg, container=st)
    st.caption(
        "Time-guard rules are NON-OVERRIDABLE in live mode by default. "
        "To disable, edit `data/risk_config.json` AND acknowledge that "
        "this voids FTMO compliance."
    )


def render_emergency_stop_section():
    st.markdown("### ⛔  Emergency Stop")
    sentinel = REPO / "data" / "EMERGENCY_STOP"
    has_stop = render_emergency_stop_indicator(REPO / "data")
    cols = st.columns(2)
    if not has_stop:
        if cols[0].button("⛔  Activate EMERGENCY_STOP",
                              type="primary",
                              use_container_width=True,
                              key="ar_estop_on"):
            confirm_key = "ar_estop_confirm"
            st.session_state[confirm_key] = True
        if st.session_state.get("ar_estop_confirm"):
            st.warning("Confirm: this REFUSES all live orders until removed.")
            cc = st.columns(2)
            if cc[0].button("✅  Yes, activate", key="ar_estop_yes"):
                sentinel.touch()
                st.session_state["ar_estop_confirm"] = False
                st.rerun()
            if cc[1].button("Cancel", key="ar_estop_no"):
                st.session_state["ar_estop_confirm"] = False
                st.rerun()
    else:
        if cols[0].button("✅  Lift EMERGENCY_STOP",
                              type="secondary",
                              use_container_width=True,
                              key="ar_estop_off"):
            sentinel.unlink(missing_ok=True)
            st.toast("EMERGENCY_STOP removed")
            st.rerun()


def render_risk_caps_editor(cfg):
    st.markdown("### ⚙️  Risk caps (data/risk_config.json)")
    st.caption(
        "Edits save to `data/risk_config.json`. The dashboard re-reads on "
        "every page load."
    )
    cur = cfg.raw
    with st.form("risk_caps_form"):
        cols = st.columns(3)
        new_daily = cols[0].number_input("daily_loss_cap_pct",
                                            value=float(cur["daily_loss_cap_pct"]),
                                            step=0.5, format="%.2f")
        new_consec = int(cols[1].number_input("max_consecutive_losses",
                                                  value=int(cur["max_consecutive_losses"]),
                                                  step=1))
        new_max_open = int(cols[2].number_input("max_open_positions",
                                                    value=int(cur["max_open_positions"]),
                                                    step=1))
        cols2 = st.columns(2)
        new_no_entry = int(cols2[0].number_input("no_entry_minutes_before_close",
                                                    value=int(cur["no_entry_minutes_before_close"]),
                                                    step=5))
        new_buffer = int(cols2[1].number_input("flat_buffer_minutes",
                                                  value=int(cur["flat_buffer_minutes"]),
                                                  step=1))
        if st.form_submit_button("💾  Save"):
            new_cfg = dict(cur)
            new_cfg["daily_loss_cap_pct"] = new_daily
            new_cfg["max_consecutive_losses"] = new_consec
            new_cfg["max_open_positions"] = new_max_open
            new_cfg["no_entry_minutes_before_close"] = new_no_entry
            new_cfg["flat_buffer_minutes"] = new_buffer
            try:
                save_config(new_cfg)
                st.success("Saved. Re-run other pages to pick up the change.")
            except Exception as e:
                st.error(f"Save failed: {e}")


def render_ftmo_pass_rate_widget():
    """Placeholder for the Phase 25 simulator. We render the panel so the
    structure is present; the real Monte-Carlo will land in core/ftmo_simulator.py.
    """
    st.markdown("### 🎲  FTMO Pass-Rate Simulator (preview)")
    cols = st.columns([1, 2])
    cols[0].metric("P(pass in 30 days)", "—",
                    help="Coming in Phase 25 — `make ftmo-sim`")
    cols[1].caption(
        "Monte-Carlo bootstrap from each strategy's OOS R-distribution. "
        "Implementation lives in `core/ftmo_simulator.py` (next milestone). "
        "Until then, run a single backtest of your portfolio per-strategy "
        "and inspect the R-distribution histogram on Page 4."
    )


def main():
    st.set_page_config(page_title="Account & Risk", page_icon="⚙️", layout="wide")
    st.title("⚙️  Account & Risk")
    cfg = load_config()
    render_account_section()
    render_ftmo_progress(cfg)
    render_per_strategy_risk()
    render_time_guards(cfg)
    render_emergency_stop_section()
    render_risk_caps_editor(cfg)
    render_ftmo_pass_rate_widget()


main()
