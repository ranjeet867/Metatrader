"""
account_switcher.py — top-of-page account dropdown + add-account form.
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core import account_manager


SS_KEY = "_active_account_login"


def _ensure_session_active(default_login: int | None) -> None:
    if SS_KEY not in st.session_state and default_login is not None:
        st.session_state[SS_KEY] = default_login


def get_active_login() -> int | None:
    return st.session_state.get(SS_KEY)


def set_active_login(login: int) -> None:
    st.session_state[SS_KEY] = login


def render(*, container=None):
    """Render the switcher UI. Persists selection in st.session_state."""
    target = container or st
    accounts = account_manager.list_accounts()
    if not accounts:
        target.warning(
            "No accounts configured. Click **➕ Add Account** below "
            "to add your FTMO login."
        )
    else:
        _ensure_session_active(accounts[0].login)
        labels = {
            a.login: f"{a.alias}  ·  #{a.login}  ·  {a.type}"
            for a in accounts
        }
        choice = target.selectbox(
            "Active account",
            options=[a.login for a in accounts],
            format_func=lambda lg: labels.get(lg, str(lg)),
            index=([a.login for a in accounts].index(get_active_login())
                    if get_active_login() in [a.login for a in accounts]
                    else 0),
            key="ac_switcher",
        )
        set_active_login(choice)

    with target.expander("➕  Add Account", expanded=not accounts):
        with st.form("ac_add_form"):
            cols = st.columns(2)
            login = int(cols[0].number_input("MT5 login", value=0,
                                                  step=1, key="ac_add_login"))
            alias = cols[1].text_input("alias",
                                          value="My Account", key="ac_add_alias")
            cols2 = st.columns(3)
            broker = cols2[0].selectbox("broker",
                                            ["FTMO", "TopStep", "MyForexFunds",
                                             "Other"], key="ac_add_broker")
            type_ = cols2[1].selectbox(
                "type",
                ["challenge_100k", "verification_100k", "funded_100k",
                  "challenge_50k", "live_demo", "live_real"],
                key="ac_add_type",
            )
            phase = int(cols2[2].number_input("ftmo_phase",
                                                value=1, step=1,
                                                min_value=0, max_value=2,
                                                key="ac_add_phase"))
            tz = st.text_input("user timezone (IANA)",
                                  value="Asia/Kolkata", key="ac_add_tz")
            if st.form_submit_button("Add account"):
                if login <= 0:
                    st.error("login must be > 0")
                else:
                    account_manager.add_account(
                        login=login, alias=alias, broker=broker,
                        type=type_, ftmo_phase=phase, user_tz=tz,
                    )
                    set_active_login(login)
                    st.success(f"Added {alias} ({login}). Re-run page.")
                    st.rerun()
