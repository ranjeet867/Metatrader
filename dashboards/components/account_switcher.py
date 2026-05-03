"""
account_switcher.py — top-of-page account dropdown + add-account form
with two ways to add an account:

  1. 🔍 **Auto-detect from MT5**  — probe the running bridge, grab login /
     broker / server / balance / leverage, infer type + local timezone,
     show a confirm card, save in one click.

  2. ✏️  **Manual entry**  — for accounts not currently connected (e.g.
     adding a second login for later) or when the bridge is offline.

The auto-detect path is preferred — it eliminates the "login=1" footgun
entirely and gets the broker tz right without the user looking it up.
"""
from __future__ import annotations

import streamlit as st

from core import account_manager
from core.account_detect import (
    DetectedAccount,
    detect_account_via_bridge,
)


SS_KEY = "_active_account_login"
_DETECTED_KEY = "_detected_account"   # cached DetectedAccount across reruns

# Sensible per-type defaults for FTMO challenge baselines
TYPE_TO_BASELINE = {
    "challenge_100k": 100_000.0,
    "verification_100k": 100_000.0,
    "funded_100k": 100_000.0,
    "challenge_50k": 50_000.0,
    "verification_50k": 50_000.0,
    "funded_50k": 50_000.0,
    "challenge_25k": 25_000.0,
    "challenge_200k": 200_000.0,
    "challenge_10k": 10_000.0,
    "live_demo": 0.0,
    "live_real": 0.0,
}


def _ensure_session_active(default_login: int | None) -> None:
    if SS_KEY not in st.session_state and default_login is not None:
        st.session_state[SS_KEY] = default_login


def get_active_login() -> int | None:
    return st.session_state.get(SS_KEY)


def set_active_login(login: int) -> None:
    st.session_state[SS_KEY] = login


def _detected_card_html(d: DetectedAccount) -> str:
    return (
        f"<div style='background:#0f1419;border:1px solid #1f2937;"
        f"border-radius:8px;padding:14px 18px;margin-top:8px;'>"
        f"<div style='color:#9ca3af;font-size:0.74rem;text-transform:uppercase;"
        f"letter-spacing:0.08em;margin-bottom:8px;'>"
        f"Detected from MT5 bridge</div>"
        f"<div style='font-family:ui-monospace,Menlo,monospace;"
        f"font-size:0.92rem;font-variant-numeric:tabular-nums;line-height:1.6;'>"
        f"<b style='color:#e5e7eb;'>{d.alias}</b><br>"
        f"login <code>{d.login}</code> · server <code>{d.server or '?'}</code>"
        f" · company <code>{d.company or '?'}</code><br>"
        f"<span style='color:#9ca3af;'>"
        f"holder</span> {d.name or '—'}"
        f"  ·  <span style='color:#9ca3af;'>type</span> {d.type}"
        f"  ·  <span style='color:#9ca3af;'>mode</span> {d.trade_mode_label}<br>"
        f"<span style='color:#9ca3af;'>balance</span> ${d.balance:,.2f}"
        f"  ·  <span style='color:#9ca3af;'>equity</span> ${d.equity:,.2f}"
        f"  ·  <span style='color:#9ca3af;'>leverage</span> 1:{d.leverage}"
        f"  ·  <span style='color:#9ca3af;'>currency</span> {d.currency}<br>"
        f"<span style='color:#9ca3af;'>your timezone</span> "
        f"<code>{d.user_tz}</code> "
        f"<span style='color:#6b7280;'>(detected from system)</span>"
        f"</div></div>"
    )


def _render_auto_detect(target) -> None:
    """The auto-detect block at the top of the expander."""
    cols = target.columns([2, 5])
    if cols[0].button("🔍  Auto-detect from MT5",
                       use_container_width=True,
                       help="Probe the running bridge for the connected "
                            "account. Skips the login lookup entirely."):
        # Lazy bridge import — not needed until clicked
        from core.mt5_account import MT5AccountClient
        bridge = MT5AccountClient()
        with st.spinner("Querying MT5 bridge…"):
            d = detect_account_via_bridge(bridge)
        if d is None:
            cols[1].error(
                "Could not detect — bridge offline or no MT5 terminal "
                "connected. Use **manual entry** below."
            )
            st.session_state.pop(_DETECTED_KEY, None)
        elif not d.is_valid:
            cols[1].error(
                f"Bridge returned login `{d.login}` which doesn't look real. "
                f"Check that MT5 terminal is logged in."
            )
            st.session_state.pop(_DETECTED_KEY, None)
        else:
            st.session_state[_DETECTED_KEY] = d
            cols[1].success(
                f"Found {d.broker} account #{d.login}. Review below "
                f"and click **Save** to add it."
            )

    detected: DetectedAccount | None = st.session_state.get(_DETECTED_KEY)
    if detected is None:
        return

    # Show the detected card
    target.markdown(_detected_card_html(detected), unsafe_allow_html=True)

    # Allow the user to tweak alias / type / baseline / tz before saving.
    with target.form("ac_save_detected"):
        cols2 = st.columns(2)
        alias = cols2[0].text_input("Alias", value=detected.alias)
        type_ = cols2[1].selectbox(
            "Type", list(TYPE_TO_BASELINE.keys()),
            index=(list(TYPE_TO_BASELINE.keys()).index(detected.type)
                   if detected.type in TYPE_TO_BASELINE else 0),
        )
        cols3 = st.columns(3)
        tz = cols3[0].text_input("Your timezone (IANA)", value=detected.user_tz,
                                  help="Detected from your system. Edit if wrong.")
        baseline = float(cols3[1].number_input(
            "Risk baseline equity ($)", value=float(detected.risk_baseline_equity),
            step=1_000.0, min_value=0.0,
            help="FTMO challenges start at the published size. For accounts "
                 "already past the loss limit being reused as a sandbox, "
                 "set this to current equity.",
        ))
        phase = int(cols3[2].number_input(
            "FTMO phase", value=1, step=1, min_value=0, max_value=2,
            help="1 = Challenge, 2 = Verification, 0 = Funded/non-FTMO.",
        ))
        save_clicked = st.form_submit_button(
            "💾  Save detected account", type="primary")
        cancel_clicked = st.form_submit_button("Cancel")
        if save_clicked:
            account_manager.add_account(
                login=detected.login, alias=alias,
                broker=detected.broker, type=type_,
                ftmo_phase=phase, user_tz=tz,
                risk_baseline_equity=baseline,
            )
            set_active_login(detected.login)
            st.session_state.pop(_DETECTED_KEY, None)
            st.success(f"Added {alias} (#{detected.login}). Re-running…")
            st.rerun()
        if cancel_clicked:
            st.session_state.pop(_DETECTED_KEY, None)
            st.rerun()


def _render_manual(target) -> None:
    """Original manual-entry form."""
    target.markdown("---")
    target.markdown("**…or enter manually**")
    with st.form("ac_add_form_manual", clear_on_submit=False):
        cols = st.columns([2, 3])
        login_text = cols[0].text_input(
            "MT5 login (6-9 digits)",
            value="", max_chars=12, key="ac_add_login_text",
            placeholder="e.g. 1234567890",
            help="The numeric login your broker issued. Not your email.",
        )
        alias = cols[1].text_input(
            "Alias", value="FTMO 100k Challenge", key="ac_add_alias",
            help="Display name for this account in the switcher.",
        )
        cols2 = st.columns([2, 2, 2])
        broker = cols2[0].selectbox(
            "Broker",
            ["FTMO", "TopStep", "MyForexFunds", "The5ers", "FundedNext",
             "E8", "SmartProp", "Other"],
            key="ac_add_broker",
        )
        type_ = cols2[1].selectbox(
            "Type", list(TYPE_TO_BASELINE.keys()),
            index=0, key="ac_add_type",
        )
        phase = int(cols2[2].number_input(
            "FTMO phase", value=1, step=1, min_value=0, max_value=2,
            key="ac_add_phase",
            help="1 = Challenge, 2 = Verification, 0 = Funded/non-FTMO",
        ))
        cols3 = st.columns([2, 2])
        tz = cols3[0].text_input(
            "Your timezone (IANA)", value="Asia/Kolkata", key="ac_add_tz",
            help="Used to display FTMO close countdowns in local time.",
        )
        inferred = TYPE_TO_BASELINE.get(type_, 100_000.0)
        baseline_input = float(cols3[1].number_input(
            "Risk baseline equity ($)",
            value=float(inferred), step=1_000.0, min_value=0.0,
            key="ac_add_baseline",
            help=("Anchor for FTMO buffer math. Use 0 to infer from "
                  "type. For an account already past the loss limit "
                  "being reused as a sandbox, set to current equity."),
        ))

        if st.form_submit_button("Add account", type="primary"):
            stripped = (login_text or "").strip()
            if not stripped.isdigit() or len(stripped) < 5:
                st.error(
                    "MT5 login must be at least 5 digits. Check your "
                    "broker portal for the numeric login."
                )
                return
            login_int = int(stripped)
            account_manager.add_account(
                login=login_int, alias=alias, broker=broker,
                type=type_, ftmo_phase=phase, user_tz=tz,
                risk_baseline_equity=baseline_input,
            )
            set_active_login(login_int)
            st.success(f"Added {alias} (#{login_int}). Re-running…")
            st.rerun()


def render(*, container=None) -> None:
    """Render the switcher. Persists selection in st.session_state."""
    target = container or st
    accounts = account_manager.list_accounts()
    if not accounts:
        target.warning(
            "No accounts configured. Click **🔍 Auto-detect from MT5** "
            "below to grab the connected account in one click."
        )
    else:
        _ensure_session_active(accounts[0].login)
        labels = {
            a.login: f"{a.alias}  ·  #{a.login}  ·  {a.type}  ·  {a.user_tz}"
            for a in accounts
        }
        cur_login_list = [a.login for a in accounts]
        active = get_active_login()
        idx = cur_login_list.index(active) if active in cur_login_list else 0
        choice = target.selectbox(
            "Active account",
            options=cur_login_list,
            format_func=lambda lg: labels.get(lg, str(lg)),
            index=idx,
            key="ac_switcher",
        )
        set_active_login(choice)

    with target.expander("➕  Add account", expanded=not accounts):
        _render_auto_detect(st)
        _render_manual(st)
