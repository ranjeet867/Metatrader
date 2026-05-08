"""
go_live_modal.py — pre-flight checklist + DEPLOY-typed confirmation.

INVARIANT-11: live execution requires ALL pre-flight checks pass AND the
user typed `DEPLOY` exactly. No bypass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import streamlit as st

from core import account_manager
from core.deployment import Deployment


CONFIRM_PHRASE = "DEPLOY"


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str
    fix_hint: str | None = None


def _build_checks(*, login: int, dep: Deployment, cfg, parity_gate,
                   risk_tracker, time_guard_cfg, ftmo_clock,
                   account_baseline: float = 100_000) -> list[Check]:
    """All 8 INVARIANT-6 + INVARIANT-11 pre-flight checks."""
    out: list[Check] = []

    # 1. EMERGENCY_STOP absent
    es = account_manager.emergency_stop_active()
    out.append(Check(
        label="EMERGENCY_STOP file absent",
        ok=not es,
        detail="absent" if not es else "PRESENT — clear via the bar above",
        fix_hint=None if not es else "Clear the EMERGENCY_STOP banner",
    ))

    # 2. Account in allowlist
    allowed = cfg.live_safety_allowed_accounts
    in_allow = (not allowed) or login in allowed
    out.append(Check(
        label="Account in allowlist",
        ok=in_allow,
        detail=("(open allowlist)" if not allowed
                else f"login {login} {'in' if in_allow else 'NOT in'} {list(allowed)}"),
        fix_hint=None if in_allow else "Edit live_safety.allowed_accounts in risk_config.json",
    ))

    # 3. Daily loss within FTMO buffer
    daily = risk_tracker.account_daily_loss_pct() if risk_tracker else 0.0
    cap = cfg.daily_loss_cap_pct
    out.append(Check(
        label=f"Daily loss < {cap:.1f}%",
        ok=daily < cap,
        detail=f"current {daily:.2f}% / cap {cap:.2f}%",
        fix_hint=None if daily < cap else "Wait for FTMO 22:00 UTC reset",
    ))

    # 4. Reconciliation green on last backtest (session-state)
    last_bt = st.session_state.get("_last_backtest")
    if last_bt is None:
        out.append(Check(
            label="Last backtest reconciles",
            ok=False,
            detail="no backtest in this session",
            fix_hint="Run a backtest on Page 1 first",
        ))
    else:
        r = last_bt["result"]
        out.append(Check(
            label="Last backtest reconciles",
            ok=r.reconciles,
            detail=f"divergence ${abs(r.sum_realized_pnl - r.equity_curve_pnl):.4f}",
            fix_hint=None if r.reconciles else "Investigate divergence",
        ))

    # 5. Parity recent
    if parity_gate is not None:
        recent = parity_gate.is_recent(dep.strategy, max_age_hours=24)
        last = parity_gate.last_pass_for(dep.strategy)
        if last is None:
            detail = "no parity pass on record"
        else:
            ts, div = last
            detail = f"last pass {ts.strftime('%Y-%m-%d %H:%M UTC')}, div=${div:.4f}"
        out.append(Check(
            label=f"Replay parity for {dep.strategy} < 24h",
            ok=recent,
            detail=detail,
            fix_hint=None if recent else "Run replay on Page 3 → Tab A",
        ))
    else:
        out.append(Check(
            label="Replay parity recent",
            ok=False, detail="parity_gate not loaded",
        ))

    # 6. Idempotency keys reset (always true at start of a deployment)
    out.append(Check(
        label="Idempotency keys ready",
        ok=True, detail="key window is per-session",
    ))

    # 7. NOT in INVARIANT-8 no-entry window (US session close)
    from core.time_guards import in_no_entry_window
    in_win = in_no_entry_window(datetime.now(timezone.utc), time_guard_cfg)
    out.append(Check(
        label="Not in US-close no-entry window",
        ok=not in_win,
        detail="outside" if not in_win else "inside [19:30,20:00] UTC",
        fix_hint=None if not in_win else "Wait until after 20:00 UTC",
    ))

    # 8. NOT in FTMO pre-close window for this asset class
    from core.asset_class import classify
    ac = classify(dep.ticker, overrides=cfg.asset_class_overrides)
    in_pre = ftmo_clock.is_within_pre_close_window(
        datetime.now(timezone.utc), ac
    ) if ftmo_clock else False
    out.append(Check(
        label="Not in FTMO pre-close window",
        ok=not in_pre,
        detail=f"asset_class={ac}; outside" if not in_pre else "inside",
        fix_hint=None if not in_pre else "Wait for FTMO daily reset",
    ))
    return out


def render_modal(*, login: int, dep: Deployment, cfg,
                  parity_gate=None, risk_tracker=None,
                  time_guard_cfg, ftmo_clock=None,
                  account_baseline: float = 100_000):
    """Render the modal body. Returns True iff the user clicked Go Live
    AND all checks passed AND DEPLOY was typed.

    The caller is responsible for actually invoking the live executor.
    """
    st.markdown(f"### Deploy LIVE: {dep.strategy} × `{dep.ticker}` × `{dep.tf}`")
    checks = _build_checks(
        login=login, dep=dep, cfg=cfg,
        parity_gate=parity_gate, risk_tracker=risk_tracker,
        time_guard_cfg=time_guard_cfg, ftmo_clock=ftmo_clock,
        account_baseline=account_baseline,
    )
    all_ok = all(c.ok for c in checks)

    st.markdown("**Pre-flight checks**")
    for c in checks:
        emoji = "✓" if c.ok else "⛔"
        line = f"{emoji}  **{c.label}** — {c.detail}"
        if not c.ok and c.fix_hint:
            line += f"   _(fix: {c.fix_hint})_"
        st.markdown(line)

    # Risk math summary
    st.markdown("**Risk math**")
    st.markdown(
        f"- Risk per trade: **{dep.risk_pct:.2f}%** "
        f"= **${account_baseline * dep.risk_pct/100:,.0f}**"
    )
    st.markdown(
        f"- Daily cap: **{dep.daily_cap_pct:.2f}%** "
        f"= **${account_baseline * dep.daily_cap_pct/100:,.0f}**"
    )

    # Confirm field
    st.markdown("---")
    confirm = st.text_input(
        f"Type `{CONFIRM_PHRASE}` exactly to confirm",
        value="", key=f"glm_confirm_{login}_{dep.deployment_id}",
    )
    typed = confirm == CONFIRM_PHRASE

    cols = st.columns(2)
    cancel = cols[0].button("Cancel", key=f"glm_cancel_{login}_{dep.deployment_id}",
                                width="stretch")
    deploy = cols[1].button(
        "🚀  Go Live",
        type="primary", width="stretch",
        disabled=not (all_ok and typed),
        key=f"glm_go_{login}_{dep.deployment_id}",
    )
    if cancel:
        st.session_state.pop(f"_show_go_live_modal_{dep.deployment_id}", None)
        st.rerun()
    return deploy and all_ok and typed
