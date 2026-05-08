"""
_⚙️_Settings.py — single source of truth for all editable system config.

Replaces the scattered editors that previously lived on:
  - 5_⚙️_Account_Risk.py        (risk_config.json edits)
  - 0_🚀_Operations.py          (account record + circuit breaker tab)
  - _⚡_Command_Center.py        (System Guardrails + per-deployment risk)
  - 9_🟢_Live.py / 3_🟡_Paper.py (per-deployment risk_pct/daily_cap edits)

Every writeable field is shown ONCE in this page. Old pages are
deprecated to read-only views that link here.

Sections:
  A. Account-wide risk caps      (risk_config.json)
  B. Circuit breaker             (per-account circuit_breaker.json)
  C. Account record              (alias, equity baseline, targets)
  D. Per-deployment caps         (table — edit row → save deployments.json)
  E. Time guards                 (weekend-flat, no-entry, session close)
  F. Cost model                  (READ-ONLY — code constants)
  G. System info                 (schema versions, file paths, freshness)

Underscore prefix in filename puts this page above default-numbered
pages in Streamlit's sidebar — settings are findable without scrolling.
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

from core import (   # noqa: E402
    account_manager,
    circuit_breaker as cb_mod,
    config as risk_config_mod,
    cost_defaults,
    deployment as dep_mod,
    system_config,
)
from dashboards.components import theme   # noqa: E402

# ─── Page setup ────────────────────────────────────────────────────────
st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")
theme.inject_css()   # was theme.apply_theme() — function is named inject_css

st.title("⚙️ Settings")
st.caption(
    "Single source of truth for every editable config in the system. "
    "All writes funnel through this page — older pages are deprecated "
    "to read-only views. After saving, changes propagate within ~60s "
    "(SystemConfig cache TTL)."
)

# ─── Resolve active account ──────────────────────────────────────────────
accounts = account_manager.list_accounts()
if not accounts:
    st.error(
        "No accounts configured. Add one on the **Operations** page first."
    )
    st.stop()
account_options = {f"{a.alias or 'unnamed'} (#{a.login})": a for a in accounts}
selected_label = st.sidebar.selectbox(
    "Account", list(account_options.keys()),
    key="_settings_account_selector",
)
active = account_options[selected_label]
login = active.login

# Loaded once; sub-sections render against this snapshot. After any
# save we invalidate + rerun so the next render sees fresh data.
sysc = system_config.load_system_config(login)


def _section_header(letter: str, title: str, subtitle: str = "") -> None:
    """Visual section divider — letter + title + optional subtitle."""
    st.markdown("---")
    st.markdown(f"### {letter}. {title}")
    if subtitle:
        st.caption(subtitle)


def _save_and_rerun(login_id: int, message: str) -> None:
    """After any successful save: invalidate the SystemConfig cache so
    the next read picks up the change immediately, toast the user, and
    rerun the page so the form reflects the new state."""
    system_config.invalidate(login_id)
    try:
        st.toast(message, icon="✅")
    except Exception:
        # Older streamlit versions may not have st.toast
        st.success(message)
    import time as _t
    _t.sleep(0.4)
    st.rerun()


# ═══════════════════════════════════════════════════════════════════════
# A. ACCOUNT-WIDE RISK CAPS  (writes risk_config.json)
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "A", "Account-wide risk caps",
    "These apply ACROSS all deployments. The runner reads them on every "
    "tick and the LiveExecutor enforces them as pre-flight gates. "
    "File: `data/risk_config.json`",
)

with st.form("settings_account_risk_form", clear_on_submit=False):
    cur = sysc.account_risk
    cols = st.columns(3)
    with cols[0]:
        daily_loss_cap = st.number_input(
            "Daily loss cap (% of starting balance)",
            min_value=0.1, max_value=10.0,
            value=float(cur.daily_loss_cap_pct), step=0.1, format="%.2f",
            help="Account-wide accumulated daily-risk cap. The runner "
                  "BLOCKS new opens when today's accumulated risk would "
                  "exceed this percentage of starting balance. FTMO "
                  "challenges typically allow 5%; conservative is 0.5-1%.",
        )
        max_consec = st.number_input(
            "Max consecutive losses",
            min_value=1, max_value=50,
            value=int(cur.max_consecutive_losses), step=1,
            help="Circuit-breaker trip threshold. After N consecutive "
                  "losing trades the account-wide circuit breaker halts "
                  "all opens until manual reset.",
        )
        max_open = st.number_input(
            "Max simultaneous open positions",
            min_value=1, max_value=20,
            value=int(cur.max_open_positions), step=1,
            help="Hard cap on positions open at once across the entire "
                  "account. Prevents over-concentration if multiple "
                  "deployments fire on the same volatility spike.",
        )
    with cols[1]:
        no_entry = st.number_input(
            "No-entry window (min before close)",
            min_value=0, max_value=120,
            value=int(cur.no_entry_minutes_before_close), step=5,
            help="Block new opens during the last N minutes of the US "
                  "session. Prevents 'open at 19:50, weekend-flatted at "
                  "19:55' commission bleed. 30 min is conservative.",
        )
        flat_buffer = st.number_input(
            "Flat-buffer (min before close)",
            min_value=0, max_value=30,
            value=int(cur.flat_buffer_minutes), step=1,
            help="Force-close existing positions this many minutes "
                  "before US session close. 5 min is FTMO-safe.",
        )
        weekend_flat = st.checkbox(
            "Weekend flat (force-close all positions Friday)",
            value=bool(cur.weekend_flat_all),
            help="Closes EVERY open position at Friday `us_session_close "
                  "- flat_buffer` UTC. Prevents Sunday-open gap risk.",
        )
    with cols[2]:
        us_close = st.text_input(
            "US session close (UTC HH:MM)",
            value=cur.us_session_close_utc,
            help="The reference timestamp for daily-flat / weekend-flat. "
                  "FTMO uses 20:00 UTC year-round (no DST). Update only "
                  "if your broker uses different hours.",
        )
        ftmo_reset = st.text_input(
            "FTMO daily reset (UTC HH:MM)",
            value=cur.ftmo_daily_reset_utc,
            help="Time when FTMO rolls the daily P&L counter. The "
                  "daily_risk_accumulator resets at this hour.",
        )
        # daily_close_flat_classes is a list — show as multiselect
        flat_classes_options = ["fx", "stock", "index", "metal", "energy"]
        current_flat = list(cur.daily_close_flat_classes)
        flat_classes = st.multiselect(
            "Daily-flat asset classes",
            options=flat_classes_options,
            default=[c for c in current_flat
                      if c in flat_classes_options],
            help="Which asset classes get force-flatted at weekday close. "
                  "FX runs 24/5 so usually empty. Stocks & indices have "
                  "a defined session — close them.",
        )
    # ── Adaptive-risk auto-mode ─────────────────────────────────────
    st.markdown("---")
    st.markdown("**🤖 Adaptive risk auto-mode**")
    st.caption(
        "When ON, the runner ignores the per-deployment static risk_pct "
        "and computes per-trade risk dynamically from your remaining "
        "FTMO buffer (smallest of: daily-soft, daily-hard, total-floor) "
        "÷ target trades/day. Risk SHRINKS as buffer thins. "
        "Static risk_pct becomes the upper cap. "
        "**Recovery factor**: when account is in fragile recovery (91k→93k→91k "
        "oscillations) risk is halved; when in stable peak (>1% above baseline "
        "for 1+ hours) risk is multiplied by 1.5×."
    )
    ar_cols = st.columns(3)
    with ar_cols[0]:
        use_adaptive = st.checkbox(
            "Enable adaptive risk auto-mode",
            value=bool(getattr(cur, "use_adaptive_risk", False)),
            help="OFF = static per-deployment risk_pct (legacy). "
                  "ON = compute risk_pct each tick from FTMO buffer.",
        )
    with ar_cols[1]:
        daily_soft_cap = float(st.number_input(
            "Daily soft cap (% — your comfort)",
            min_value=0.5, max_value=10.0,
            value=float(getattr(cur, "daily_soft_cap_pct", 2.0)),
            step=0.5, format="%.1f",
            help="Your personal daily-loss limit (more conservative than "
                  "FTMO's 5% hard cap). 2% = lose 2% × baseline before "
                  "the runner blocks new opens. Gives you 5 bad days "
                  "before hitting the FTMO breach.",
        ))
    with ar_cols[2]:
        target_tpd = int(st.number_input(
            "Target trades per day (across ALL deployments)",
            min_value=1, max_value=50,
            value=int(getattr(cur, "target_trades_per_day", 5)),
            step=1,
            help="Per-trade risk = remaining_buffer ÷ this. 5 = $400 "
                  "per trade when buffer is $2k. Fewer trades = bigger "
                  "size each. Match this to your strategies' actual "
                  "fire rate (Catalog shows trades/day per cell).",
        ))

    saved = st.form_submit_button("💾 Save account-wide risk caps",
                                    type="primary",
                                    width="stretch")
    if saved:
        try:
            existing = json.loads(
                Path(REPO / "data" / "risk_config.json").read_text()
            )
            existing.update({
                "daily_loss_cap_pct": float(daily_loss_cap),
                "max_consecutive_losses": int(max_consec),
                "max_open_positions": int(max_open),
                "no_entry_minutes_before_close": int(no_entry),
                "flat_buffer_minutes": int(flat_buffer),
                "weekend_flat_all": bool(weekend_flat),
                "us_session_close_utc": str(us_close),
                "ftmo_daily_reset_utc": str(ftmo_reset),
                "daily_close_flat_classes": list(flat_classes),
                # Adaptive-risk fields
                "use_adaptive_risk": bool(use_adaptive),
                "daily_soft_cap_pct": float(daily_soft_cap),
                "target_trades_per_day": int(target_tpd),
            })
            risk_config_mod.save_config(
                existing,
                path=REPO / "data" / "risk_config.json",
            )
            _save_and_rerun(
                login,
                "Account-wide risk caps + adaptive-risk saved",
            )
        except Exception as e:
            st.error(f"Save failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# B. CIRCUIT BREAKER  (writes data/accounts/<login>/circuit_breaker.json)
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "B", "Circuit breaker (per-account)",
    "FTMO-style HALT rules that pause new opens when daily/total "
    "loss thresholds breach. Same file edited by Operations + Command "
    "Center previously — now ONE editor here. "
    f"File: `data/accounts/{login}/circuit_breaker.json`",
)

cb_path = account_manager.ACCOUNTS_DIR / str(login) / "circuit_breaker.json"
try:
    cb_existing = cb_mod.load_config(login)
    cb_existing_dict = {
        "daily_loss_dollars":
            float(getattr(cb_existing, "daily_loss_dollars", 0.0)),
        "total_loss_dollars":
            float(getattr(cb_existing, "total_loss_dollars", 0.0)),
        "daily_target_dollars":
            float(getattr(cb_existing, "daily_target_dollars", 0.0)),
        "total_target_dollars":
            float(getattr(cb_existing, "total_target_dollars", 0.0)),
        "max_consec_losses":
            int(getattr(cb_existing, "max_consec_losses", 3)),
        "max_open_positions":
            int(getattr(cb_existing, "max_open_positions", 3)),
        "halt_on_breach":
            bool(getattr(cb_existing, "halt_on_breach", True)),
    }
except Exception as e:
    st.info(f"Circuit breaker config not yet saved for this account "
            f"({e}). Saving below will create the file.")
    cb_existing_dict = {
        "daily_loss_dollars": 5_000.0,
        "total_loss_dollars": 10_000.0,
        "daily_target_dollars": 0.0,
        "total_target_dollars": 8_000.0,
        "max_consec_losses": 3,
        "max_open_positions": 3,
        "halt_on_breach": True,
    }

with st.form("settings_cb_form"):
    c1, c2 = st.columns(2)
    with c1:
        cb_daily_loss = st.number_input(
            "Daily loss limit ($)",
            min_value=0.0, value=cb_existing_dict["daily_loss_dollars"],
            step=100.0, format="%.0f",
            help="Halt all new opens once today's realized losses exceed "
                  "this dollar amount. FTMO challenges typically use 5% "
                  "of $100k = $5,000.",
        )
        cb_total_loss = st.number_input(
            "Total loss limit ($)",
            min_value=0.0, value=cb_existing_dict["total_loss_dollars"],
            step=100.0, format="%.0f",
            help="Halt at this lifetime drawdown. FTMO uses 10% = $10,000.",
        )
        cb_max_consec = st.number_input(
            "Max consecutive losses",
            min_value=1, value=cb_existing_dict["max_consec_losses"],
            step=1,
        )
        cb_max_open = st.number_input(
            "Max open positions (CB)",
            min_value=1, value=cb_existing_dict["max_open_positions"],
            step=1,
        )
    with c2:
        cb_daily_target = st.number_input(
            "Daily profit target ($, 0 = none)",
            min_value=0.0, value=cb_existing_dict["daily_target_dollars"],
            step=100.0, format="%.0f",
            help="Optional: halt opens once today's realized profit "
                  "exceeds this. Stops 'giving back gains' patterns.",
        )
        cb_total_target = st.number_input(
            "Total profit target ($, 0 = none)",
            min_value=0.0, value=cb_existing_dict["total_target_dollars"],
            step=100.0, format="%.0f",
            help="Halt once lifetime profit reaches FTMO challenge "
                  "target. $8,000 for an 8% phase-1 challenge.",
        )
        cb_halt = st.checkbox(
            "Halt on breach (vs. just alarm)",
            value=cb_existing_dict["halt_on_breach"],
            help="When True, the runner physically blocks new opens "
                  "after a breach. When False, it only logs CRITICAL "
                  "(useful during testing).",
        )
    saved_cb = st.form_submit_button(
        "💾 Save circuit breaker config",
        type="primary", width="stretch",
    )
    if saved_cb:
        try:
            new_cb = cb_mod.CircuitConfig(
                daily_loss_dollars=(float(cb_daily_loss)
                                       if cb_daily_loss > 0 else None),
                total_loss_dollars=(float(cb_total_loss)
                                       if cb_total_loss > 0 else None),
                daily_target_dollars=(float(cb_daily_target)
                                          if cb_daily_target > 0 else None),
                total_target_dollars=(float(cb_total_target)
                                          if cb_total_target > 0 else None),
                max_consec_losses=int(cb_max_consec),
                max_open_positions=int(cb_max_open),
                halt_on_breach=bool(cb_halt),
            )
            cb_mod.save_config(login, new_cb)
            _save_and_rerun(login, "Circuit breaker saved")
        except Exception as e:
            st.error(f"Save failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# C. ACCOUNT RECORD  (alias, baseline equity, FTMO targets)
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "C", "Account record",
    "Account metadata + FTMO challenge baselines. The Profit-progress "
    "tile + Total-loss buffer are computed from these.",
)

with st.form("settings_account_form"):
    c1, c2, c3 = st.columns(3)
    with c1:
        alias_in = st.text_input(
            "Alias",
            value=str(active.alias or ""),
            help="Display name for this account (e.g. 'FTMO Challenge 100K').",
        )
        baseline_eq = st.number_input(
            "Risk baseline equity ($)",
            min_value=0.0, step=100.0, format="%.0f",
            value=float(active.risk_baseline_equity or 100_000.0),
            help="Used as the denominator for risk-pct sizing. For FTMO "
                  "challenges this is the starting balance ($100k typical).",
        )
    with c2:
        acct_daily_loss = st.number_input(
            "Account daily-loss cap (%)",
            min_value=0.0, max_value=20.0, step=0.1, format="%.2f",
            value=float(getattr(active, "daily_loss_cap_pct", 5.0)),
            help="FTMO daily cap. 5% = $5,000 on $100k. NOTE: this "
                  "overlaps with section A's daily_loss_cap_pct. Use "
                  "the most CONSERVATIVE of the two — both are enforced.",
        )
        acct_total_loss = st.number_input(
            "Account total-loss cap (%)",
            min_value=0.0, max_value=30.0, step=0.1, format="%.2f",
            value=float(getattr(active, "total_loss_cap_pct", 10.0)),
        )
    with c3:
        profit_target = st.number_input(
            "Profit target (%)",
            min_value=0.0, max_value=50.0, step=0.5, format="%.2f",
            value=float(getattr(active, "profit_target_pct", 8.0)),
            help="FTMO challenge target — 8% phase 1, 5% phase 2.",
        )
        days_required = st.number_input(
            "Min trading days required",
            min_value=0, max_value=60, step=1,
            value=int(getattr(active, "days_required", 4)),
            help="FTMO Phase-1 requires ≥4 days; some firms require 10.",
        )
    saved_acct = st.form_submit_button(
        "💾 Save account record", type="primary",
        width="stretch",
    )
    if saved_acct:
        try:
            account_manager.update_account(
                login,
                alias=alias_in,
                risk_baseline_equity=float(baseline_eq),
                daily_loss_cap_pct=float(acct_daily_loss),
                total_loss_cap_pct=float(acct_total_loss),
                profit_target_pct=float(profit_target),
                days_required=int(days_required),
            )
            _save_and_rerun(login, "Account record saved")
        except Exception as e:
            st.error(f"Save failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# D. PER-DEPLOYMENT CAPS  (table — edit row → save deployments.json)
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "D", "Per-deployment caps",
    "Each deployment's risk_pct / daily_cap_pct / max_lots / "
    "max_money_risk_usd. Pre-fix these were edited from 8 different "
    "places — Live, Paper, Operations, Command Center, Composer, "
    "Compare, Library — and could disagree. Now: ONE editor here.",
)

deployments = dep_mod.load_deployments(login)
if not deployments:
    st.info("No deployments yet. Create one via Composer or Strategy "
            "Library.")
else:
    # Show current state as a table the user can edit in place.
    rows = [{
        "deployment_id": d.deployment_id,
        "status": d.status,
        "ticker": d.ticker,
        "tf": d.tf,
        "risk_pct": float(d.risk_pct or 0.0),
        "daily_cap_pct": float(d.daily_cap_pct or 0.0),
        "max_lots": float(d.max_lots or 0.0),
        "max_money_risk_usd": float(d.max_money_risk_usd or 0.0),
    } for d in deployments]
    df = pd.DataFrame(rows)
    edited = st.data_editor(
        df,
        disabled=["deployment_id", "status", "ticker", "tf"],
        column_config={
            "risk_pct": st.column_config.NumberColumn(
                "Risk %", min_value=0.0, max_value=5.0, step=0.05,
                format="%.2f",
            ),
            "daily_cap_pct": st.column_config.NumberColumn(
                "Daily cap %", min_value=0.0, max_value=10.0, step=0.1,
                format="%.2f",
            ),
            "max_lots": st.column_config.NumberColumn(
                "Max lots", min_value=0.01, max_value=200.0, step=0.5,
                format="%.2f",
            ),
            "max_money_risk_usd": st.column_config.NumberColumn(
                "Max $ risk/trade", min_value=0.0, max_value=10_000.0,
                step=10.0, format="%.0f",
                help="0 = no $-cap (trust risk_pct alone)",
            ),
        },
        hide_index=True,
        width="stretch",
        key="settings_per_deployment_editor",
    )

    if st.button("💾 Save per-deployment caps",
                  type="primary", width="stretch"):
        try:
            by_id = {d.deployment_id: d for d in deployments}
            n_changed = 0
            for _, r in edited.iterrows():
                d = by_id.get(r["deployment_id"])
                if d is None:
                    continue
                changed = False
                if abs(d.risk_pct - r["risk_pct"]) > 1e-6:
                    d.risk_pct = float(r["risk_pct"])
                    changed = True
                if abs(d.daily_cap_pct - r["daily_cap_pct"]) > 1e-6:
                    d.daily_cap_pct = float(r["daily_cap_pct"])
                    changed = True
                if abs(d.max_lots - r["max_lots"]) > 1e-6:
                    d.max_lots = float(r["max_lots"])
                    changed = True
                if (abs(d.max_money_risk_usd - r["max_money_risk_usd"])
                        > 1e-6):
                    d.max_money_risk_usd = float(r["max_money_risk_usd"])
                    changed = True
                if changed:
                    n_changed += 1
            if n_changed:
                dep_mod.save_deployments(login, deployments)
                _save_and_rerun(
                    login,
                    f"Saved {n_changed} deployment(s)",
                )
            else:
                st.info("No changes to save")
        except Exception as e:
            st.error(f"Save failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# E. TIME GUARDS  (read-only summary — values come from Section A)
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "E", "Time guards (resolved)",
    "Read-only view of how the account-wide risk caps flow into "
    "TimeGuardCfg. Edit values in Section A — this view shows the "
    "resolved state.",
)

cur = sysc.account_risk
st.dataframe(
    pd.DataFrame([
        {"Setting": "Weekend flat (Friday close-all)",
         "Value": "ON" if cur.weekend_flat_all else "OFF"},
        {"Setting": "Daily-flat asset classes",
         "Value": ", ".join(cur.daily_close_flat_classes) or "(none)"},
        {"Setting": "US session close (UTC)",
         "Value": cur.us_session_close_utc},
        {"Setting": "Flat buffer (min before close)",
         "Value": f"{cur.flat_buffer_minutes} min"},
        {"Setting": "No-entry window (min before close)",
         "Value": f"{cur.no_entry_minutes_before_close} min"},
        {"Setting": "FTMO daily reset (UTC)",
         "Value": cur.ftmo_daily_reset_utc},
    ]),
    hide_index=True, width="stretch",
)


# ═══════════════════════════════════════════════════════════════════════
# F. COST MODEL  (READ-ONLY — code constants from cost_defaults.py)
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "F", "Cost model (read-only)",
    "Backtest cost assumptions — applied uniformly by Backtest page, "
    "rebaseline, sweep, run_backtest CLI, and Strategy Library. To "
    "change them edit `core/cost_defaults.py` then re-run "
    "`python scripts/rebaseline_catalog.py` so every cell re-prices.",
)
cm = sysc.cost_model
st.dataframe(
    pd.DataFrame([
        {"Setting": "Commission per round-trip ($)",
         "Value": f"${cm.commission_usd:.2f}"},
        {"Setting": "Slippage per fill (× ATR)",
         "Value": f"{cm.slippage_atr_frac:.3f}"},
        {"Setting": "Starting balance ($)",
         "Value": f"${cm.starting_balance_usd:,.0f}"},
        {"Setting": "Train/test split",
         "Value": (f"{int(cm.train_pct*100)}/{int((1-cm.train_pct)*100)}")},
        {"Setting": "Default risk-pct per trade (%)",
         "Value": f"{cm.risk_pct_default:.2f}%"},
    ]),
    hide_index=True, width="stretch",
)
st.caption(
    f"Catalog cost summary: {cost_defaults.cost_config_badge()}"
)


# ═══════════════════════════════════════════════════════════════════════
# G. SYSTEM INFO
# ═══════════════════════════════════════════════════════════════════════
_section_header(
    "G", "System info",
    "Schema versions, file paths, last-modified timestamps. Use this "
    "to debug 'is the runner reading the same file I just edited?'",
)

paths_to_check = {
    "risk_config.json":
        REPO / "data" / "risk_config.json",
    "deployments.json":
        account_manager.ACCOUNTS_DIR / str(login) / "deployments.json",
    "circuit_breaker.json":
        account_manager.ACCOUNTS_DIR / str(login) / "circuit_breaker.json",
    "daily_risk.json":
        account_manager.ACCOUNTS_DIR / str(login) / "daily_risk.json",
    "v2.db":
        account_manager.ACCOUNTS_DIR / str(login) / "v2.db",
    "runner_state.json":
        account_manager.ACCOUNTS_DIR / str(login) / "runner_state.json",
    "symbol_info.json":
        REPO / "data" / "symbol_info.json",
}
rows = []
for name, p in paths_to_check.items():
    if p.exists():
        mtime = datetime.fromtimestamp(p.stat().st_mtime,
                                          tz=timezone.utc)
        age = (datetime.now(timezone.utc) - mtime).total_seconds()
        size_kb = p.stat().st_size / 1024
        rows.append({
            "File": name,
            "Exists": "✅",
            "Last modified (UTC)": mtime.isoformat(timespec="seconds"),
            "Age": (f"{age:.0f}s" if age < 60
                     else f"{age/60:.0f}m" if age < 3600
                     else f"{age/3600:.1f}h"),
            "Size (KB)": f"{size_kb:.1f}",
        })
    else:
        rows.append({
            "File": name, "Exists": "—",
            "Last modified (UTC)": "—", "Age": "—", "Size (KB)": "—",
        })
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

st.caption(
    f"SystemConfig schema version: {sysc.schema_version} · "
    f"Cache loaded {(datetime.now().timestamp() - sysc.loaded_at_unix):.0f}s "
    f"ago (TTL 60s) · "
    f"Click any '💾 Save' button above to invalidate cache + propagate."
)
