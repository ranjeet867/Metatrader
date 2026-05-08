"""
deployment_card.py — one card per (strategy × ticker × tf × account).

Status badge, risk knobs, backtest-edge inline stats (so the operator knows
what they're deploying), then action buttons. Designed to be dense — at a
glance you can see whether the strategy has positive train + test edge and
how many OOS trades it had.

ARCHITECTURE NOTE — TWO COEXISTING APIs (intentional)
=====================================================

The component exports two public APIs by design — they serve different
pages with fundamentally different action surfaces, and merging them
would either lose features or balloon the API:

1. ``render(login=..., dep=..., on_go_live=..., ...)`` — the original
   CALLBACK-driven card. Used by the Operations page (page 0). Includes
   inline backtest-edge chip (EDGE / OOS only / negative) and 5
   callback buttons (Backtest, Paper, Go Live, Pause/Resume, Remove).
   These map onto Operations' workflow of EXPLORING + DEPLOYING new
   cells — every button needs a callback to a parent dialog. Cannot
   be replaced by render_card() without losing the Backtest dialog
   trigger and Go-Live dialog trigger.

2. ``render_card(account, deployment, mode="paper"|"live"|"readonly")``
   — the unified INLINE-BUTTON card. Used by Paper (page 3) and Live
   (page 9). Buttons fire status changes directly without callbacks
   because Paper/Live don't have multi-step dialogs — pause means
   pause, demote means demote. Same status pill + identifier strip +
   risk%/cap%/$ widgets + auto-save on both pages; only the action
   row varies by mode.

Command Center (page _⚡_Command_Center.py) does NOT render individual
cards at all — it shows summary tiles (counts, status counts,
edge-decay states). So #263 is partially N/A there.

Why the dual pattern stays
--------------------------
- Operations needs the multi-action dialog hooks; the inline-button
  pattern doesn't model "open a Backtest dialog, then open a Go-Live
  dialog after" without breaking the simple status-change semantics.
- Paper/Live deliberately have NO Backtest/Go-Live actions — those
  are Operations' job. Forcing render_card() to support both styles
  would dilute its single-purpose simplicity.

Both APIs share ``status_pill()`` so the colour-coding for IDLE / PAPER /
LIVE / PAUSED / HALTED is identical everywhere — that was the real
DRY win and it's complete.
"""
from __future__ import annotations

from typing import Any, Callable, Literal, Optional

import streamlit as st

from core import account_manager, deployment as dep_mod, edge_catalog
from core.deployment import Deployment


_BADGE_BY_STATUS = {
    "idle":   ("💤", "IDLE",   "#475569"),
    "paper":  ("🟡", "PAPER",  "#b08800"),
    "live":   ("🟢", "LIVE",   "#0c8a3a"),
    "paused": ("⏸",  "PAUSED", "#aa6a00"),
    "halted": ("⛔", "HALTED", "#aa1a1a"),
}


def _badge_html(status: str) -> str:
    icon, text, color = _BADGE_BY_STATUS.get(
        status, ("?", status.upper(), "#666"))
    return (
        f"<span style='background:{color};color:white;padding:4px 10px;"
        f"border-radius:14px;font-size:0.78rem;font-weight:600;"
        f"font-family:ui-monospace,Menlo,monospace;"
        f"letter-spacing:0.05em;'>"
        f"{icon} {text}</span>"
    )


def _edge_badge_html(es: edge_catalog.EdgeStat | None) -> str:
    """Backtest-edge chip: green if survivor, amber if one-sided, red if
    negative, grey if no data."""
    if es is None:
        return ("<span style='color:#6b7280;font-size:0.78rem;"
                "font-family:ui-monospace,Menlo,monospace;'>"
                "no backtest edge cached — run sweep_grid</span>")
    pos_train = es.train_r > 0 and es.train_pf >= 1.0
    pos_test = es.test_r > 0 and es.test_pf >= 1.0
    if pos_train and pos_test:
        bg, tag = "#15803d", "EDGE"
    elif pos_test:
        bg, tag = "#a16207", "OOS only"
    elif pos_train:
        bg, tag = "#a16207", "IS only"
    else:
        bg, tag = "#7f1d1d", "negative"
    return (
        f"<span style='background:{bg};color:white;padding:3px 8px;"
        f"border-radius:6px;font-size:0.72rem;font-weight:600;"
        f"font-family:ui-monospace,Menlo,monospace;letter-spacing:0.04em;'>"
        f"{tag}</span>"
        f"<span style='color:#cbd5e1;font-size:0.78rem;margin-left:8px;"
        f"font-family:ui-monospace,Menlo,monospace;"
        f"font-variant-numeric:tabular-nums;'>"
        f"PF train <b>{es.train_pf:.2f}</b> / test <b>{es.test_pf:.2f}</b>"
        f"  ·  R train <b>{es.train_r:+.2f}</b> / test <b>{es.test_r:+.2f}</b>"
        f"  ·  n_test <b>{es.n_test}</b>"
        f"</span>"
    )


def render(*, login: int, dep: Deployment,
           on_go_live=None, on_paper=None, on_pause=None, on_remove=None,
           on_backtest=None, container=None,
           broker_positions=None) -> None:
    """Render one deployment card.

    `broker_positions` is the cross-account snapshot from
    MT5AccountClient.positions_get() — pass it once per render pass
    so we don't make a bridge call per card. Used by the holding-badge
    helper to identify whether THIS deployment currently has an open
    position on the broker.
    """
    target = container or st
    e_stop = account_manager.emergency_stop_active()
    status = "halted" if (e_stop and dep.status == "live") else dep.status

    with target.container(border=True):
        title_cols = st.columns([7, 2])
        title_cols[0].markdown(
            f"#### `{dep.strategy}` × `{dep.ticker}` × `{dep.tf}` "
            + ("(long-only)" if dep.long_only else "(bidir)")
        )
        title_cols[1].markdown(_badge_html(status), unsafe_allow_html=True)

        # Holding badge — shows 💼 LONG @ ... +$X if currently in a trade,
        # ⚪ flat otherwise. Computed from the shared broker snapshot.
        from dashboards.components.holding_badge import render_badge
        holding_html = render_badge(dep, broker_positions or [])

        # Risk row — display + inline editor popover. Pre-fix the user
        # had to edit deployments.json manually or use the Paper/Live
        # pages just to nudge risk_pct on a card visible right here.
        risk_cols = st.columns([6, 1])
        risk_cols[0].markdown(
            f"<div style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.82rem;color:#9ca3af;margin-bottom:6px;'>"
            f"risk <b style='color:#e5e7eb;'>{dep.risk_pct:.2f}%</b>/trade  "
            f"·  daily cap "
            f"<b style='color:#e5e7eb;'>{dep.daily_cap_pct:.2f}%</b>"
            f"<br>{holding_html}"
            f"</div>",
            unsafe_allow_html=True,
        )
        with risk_cols[1].popover("⚙ Edit",
                                    use_container_width=True,
                                    help="Change per-trade risk % and "
                                          "daily cap % for this deployment. "
                                          "Saved to deployments.json on "
                                          "change."):
            st.markdown(f"##### `{dep.deployment_id}`")
            st.caption(
                "Recommended for FTMO Challenge: **0.3% risk / 1.0% daily "
                "cap**. Auto-saves on change."
            )
            new_risk = float(st.number_input(
                "Risk per trade (%)",
                value=float(dep.risk_pct),
                min_value=0.05, max_value=5.0, step=0.05,
                format="%.2f",
                key=f"op_risk_{login}_{dep.deployment_id}",
                help="Percent of account equity risked per trade. "
                      "Adaptive risk auto-mode (Settings page) may use "
                      "this as an upper cap rather than a fixed value.",
            ))
            new_cap = float(st.number_input(
                "Daily cap (%)",
                value=float(dep.daily_cap_pct),
                min_value=0.5, max_value=10.0, step=0.5,
                format="%.1f",
                key=f"op_cap_{login}_{dep.deployment_id}",
                help="Soft daily ceiling for THIS deployment's cumulative "
                      "loss. Account-wide caps in Settings still apply on top.",
            ))
            new_max_money = float(st.number_input(
                "Max $ risk per trade",
                value=float(dep.max_money_risk_usd or 0.0),
                min_value=0.0, max_value=50000.0, step=50.0,
                format="%.2f",
                key=f"op_maxmoney_{login}_{dep.deployment_id}",
                help="Hard $-ceiling that overrides risk_pct when smaller. "
                      "0 = no cap (trust risk_pct alone).",
            ))
            if (new_risk != dep.risk_pct
                    or new_cap != dep.daily_cap_pct
                    or new_max_money != (dep.max_money_risk_usd or 0.0)):
                dep.risk_pct = new_risk
                dep.daily_cap_pct = new_cap
                dep.max_money_risk_usd = new_max_money
                dep_mod.upsert_deployment(login, dep)
                st.toast(
                    f"💾 Saved {dep.deployment_id}: "
                    f"risk {new_risk:.2f}% · cap {new_cap:.1f}% · "
                    f"max ${new_max_money:,.0f}",
                    icon="✅",
                )

        # Backtest edge stats (read from docs/grid_results.md)
        try:
            strat_prefix = dep.strategy.split("_long")[0].split("_bidir")[0]
            es = edge_catalog.best_for(dep.ticker, dep.tf, strat_prefix)
        except Exception:
            es = None
        st.markdown(_edge_badge_html(es), unsafe_allow_html=True)

        # Activity meta — only show if known (no em-dash noise)
        meta_parts = []
        if dep.last_started_at_utc:
            meta_parts.append(f"started `{dep.last_started_at_utc[:16]}`")
        if dep.last_signal_at_utc:
            meta_parts.append(f"last signal `{dep.last_signal_at_utc[:16]}`")
        if meta_parts:
            st.caption("  ·  ".join(meta_parts))

        # Actions
        act = st.columns(5)
        if act[0].button("📊 Backtest",
                          key=f"bt_{login}_{dep.deployment_id}",
                          width="stretch"):
            if on_backtest:
                on_backtest(dep)
        if act[1].button("📡 Paper",
                          key=f"pp_{login}_{dep.deployment_id}",
                          width="stretch",
                          disabled=(dep.status == "live")):
            if on_paper:
                on_paper(dep)
        if act[2].button("🚀 Go Live",
                          key=f"gl_{login}_{dep.deployment_id}",
                          type="primary",
                          width="stretch",
                          disabled=e_stop or dep.status == "live"):
            if on_go_live:
                on_go_live(dep)
        pause_label = "▶ Resume" if dep.status == "paused" else "⏸ Pause"
        if act[3].button(pause_label,
                          key=f"pa_{login}_{dep.deployment_id}",
                          width="stretch",
                          disabled=(dep.status == "idle")):
            if on_pause:
                on_pause(dep)
        if act[4].button("🗑 Remove",
                          key=f"rm_{login}_{dep.deployment_id}",
                          width="stretch"):
            if on_remove:
                on_remove(dep)


# =================================================================== #
# Unified card API for Paper + Live pages                              #
# =================================================================== #
# Same border container, same identifier strip, same risk%/cap%/$     #
# widgets with auto-save, same notes caption — only the action button #
# row varies by mode. This is the DRY replacement for what used to be #
# duplicated across `3_🟡_Paper.py` and `9_🟢_Live.py`.                #
# =================================================================== #

Mode = Literal["paper", "live", "readonly"]


_STATUS_PILL_STYLES = {
    "idle":   ("#374151", "⚪ IDLE"),
    "paper":  ("#b08800", "🟡 PAPER"),
    "live":   ("#15803d", "🟢 LIVE"),
    "paused": ("#7c2d12", "⏸ PAUSED"),
    "halted": ("#7f1d1d", "⛔ HALTED"),
}


def status_pill(status: str) -> str:
    """Inline-html pill for a deployment status. Used on Paper, Live,
    and any future page that wants the same colour-coding."""
    color, label = _STATUS_PILL_STYLES.get(
        status, ("#374151", status.upper())
    )
    return (
        f"<span style='display:inline-block;background:{color};"
        f"color:white;padding:2px 8px;border-radius:10px;"
        f"font-size:0.72rem;font-weight:600;letter-spacing:0.05em;'>"
        f"{label}</span>"
    )


def _render_unified_header(
    account: Any,
    d: Deployment,
    *,
    mode: Mode,
    parity_passed: Optional[bool],
    broker_position: Any,
    base_strategy_name: Optional[str],
    on_parity_request: Optional[Callable[[dict], None]],
    key_prefix: str,
) -> None:
    """Common identifier + risk/cap/$ row with auto-save."""
    base_strategy_name = base_strategy_name or d.strategy
    head = st.columns([3, 1, 1, 1])

    # Parity badge — live only
    parity_html = ""
    if mode == "live" and parity_passed is not None:
        parity_html = " " + (
            "✅" if parity_passed
            else "<span style='color:#dc2626;font-weight:600'>"
                 "⚠ NO PARITY</span>"
        )

    # Holding badge — same component on both pages
    try:
        from dashboards.components.holding_badge import render_badge
        holding_html = render_badge(
            d, [broker_position] if broker_position else []
        )
    except Exception:
        holding_html = ""

    head[0].markdown(
        f"{status_pill(d.status)}  "
        f"&nbsp;**`{d.strategy}`**  ×  `{d.ticker}`  ×  `{d.tf}`  "
        f"({'long-only' if d.long_only else 'bidir'})"
        f"{parity_html}"
        f"<br>&nbsp;&nbsp;{holding_html}",
        unsafe_allow_html=True,
    )

    # Inline Run-parity affordance (live page only) when parity missing
    if mode == "live" and parity_passed is False and on_parity_request:
        if head[0].button(
            "🔬 Run parity now",
            key=f"{key_prefix}parity_{d.deployment_id}",
            type="primary",
        ):
            on_parity_request({
                "variant": d.strategy,
                "base": base_strategy_name,
                "ticker": d.ticker,
                "tf": d.tf,
                "long_only": d.long_only,
            })
            st.rerun()

    new_risk = float(head[1].number_input(
        "risk %", value=float(d.risk_pct),
        min_value=0.05, max_value=2.0, step=0.05,
        format="%.2f",
        key=f"{key_prefix}risk_{d.deployment_id}",
        disabled=(mode == "readonly"),
    ))
    new_cap = float(head[2].number_input(
        "daily cap %", value=float(d.daily_cap_pct),
        min_value=0.5, max_value=10.0, step=0.5,
        format="%.1f",
        key=f"{key_prefix}cap_{d.deployment_id}",
        disabled=(mode == "readonly"),
    ))

    equity = account.effective_baseline_equity
    risk_dollars = equity * new_risk / 100.0
    head[3].markdown(
        f"<div style='font-family:ui-monospace,Menlo,monospace;"
        f"font-size:0.78rem;line-height:1.5;'>"
        f"<span style='color:#9ca3af;'>$/trade</span> "
        f"<b>${risk_dollars:,.0f}</b><br>"
        f"<span style='color:#9ca3af;'>last started</span> "
        f"{(d.last_started_at_utc or '—')[:16]}</div>",
        unsafe_allow_html=True,
    )

    if mode != "readonly":
        if (new_risk != d.risk_pct) or (new_cap != d.daily_cap_pct):
            d.risk_pct = new_risk
            d.daily_cap_pct = new_cap
            dep_mod.upsert_deployment(account.login, d)


def _backtest_url_for_deployment(d: Deployment) -> str:
    """Build a /Backtest deep-link for this deployment, sourcing
    config_json from the catalog if available so the Backtest page
    reproduces the exact OOS metrics. Falls back to the deployment's
    own params dict (also valid) when no catalog row exists.

    Used by Paper / Live action rows so the user can jump straight
    from a deployment card to the strategy's backtest with one click.
    """
    from dashboards.components.backtest_link import (
        build_backtest_url_from_parts,
    )
    cfg_json = ""
    # Prefer the catalog's stored config — it's the canonical "this
    # is what the OOS metrics were computed from" string.
    try:
        from core.edge_catalog import load_catalog
        catalog = load_catalog()
        cells = catalog.get((d.ticker, d.tf), [])
        # Match on strategy name (variant or base — catalog stores
        # variant names like "donchian_55", "rsi_30_70")
        match = next((c for c in cells if c.strategy == d.strategy), None)
        if match is None:
            # second-pass: substring match (e.g. catalog has
            # "double_bottom" and deployment has "double_bottom")
            match = next(
                (c for c in cells if d.strategy in c.strategy
                 or c.strategy in d.strategy),
                None,
            )
        if match is not None and match.source_config_json:
            cfg_json = match.source_config_json
    except Exception:
        pass
    # Fallback: serialize the deployment's params dict directly.
    if not cfg_json and getattr(d, "params", None):
        try:
            import json as _json
            cfg_json = _json.dumps(d.params, sort_keys=True)
        except Exception:
            cfg_json = ""
    return build_backtest_url_from_parts(
        ticker=d.ticker, tf=d.tf, strategy=d.strategy,
        source_config_json=cfg_json,
    )


def _render_paper_actions(
    account: Any, d: Deployment, *,
    parity_passed: bool,
    key_prefix: str,
) -> None:
    # 6 columns now: Backtest deep-link first, then the existing 5.
    actions = st.columns(6)
    cur = d.status
    actions[0].link_button(
        "📊 Backtest",
        url=_backtest_url_for_deployment(d),
        width="stretch",
        help=(
            f"Open Backtest page for {d.strategy} × {d.ticker} × {d.tf} "
            f"with the catalog's exact config — see OOS metrics, "
            f"equity curve, trade list."
        ),
    )
    if actions[1].button(
        "▶ Start paper" if cur != "paper" else "⏸ Pause",
        key=f"{key_prefix}start_{d.deployment_id}",
        width="stretch",
        type=("primary" if cur != "paper" else "secondary"),
    ):
        new_status = "paper" if cur != "paper" else "paused"
        dep_mod.update_status(account.login, d.deployment_id, new_status)
        st.rerun()
    if actions[2].button(
        "■ Stop", key=f"{key_prefix}stop_{d.deployment_id}",
        width="stretch",
        disabled=(cur in ("idle", "halted")),
    ):
        dep_mod.update_status(account.login, d.deployment_id, "idle")
        st.rerun()
    # Promote → live: parity is RECOMMENDED but no longer a hard block.
    # If parity is fresh, button promotes immediately.
    # If parity is stale/missing, button promotes BUT warns + requires
    # an explicit acknowledgement checkbox so the user can override
    # consciously rather than being silently locked out.
    promote_label = (
        "🚀 Promote → live"
        if parity_passed else
        "🚀 Promote anyway ⚠"
    )
    promote_help = (
        "Move to the Live page. Pre-flight gates still apply over there."
        if parity_passed else
        f"⚠ NO recent replay-parity pass for `{d.strategy}`. "
        f"Recommended to run parity first (Strategy Library → Run "
        f"Parity), but you can override with the acknowledge checkbox."
    )
    ack_key = f"{key_prefix}ack_{d.deployment_id}"
    can_promote = parity_passed or st.session_state.get(ack_key, False)
    if not parity_passed and cur != "live":
        st.checkbox(
            f"⚠ I acknowledge `{d.strategy}` has no recent parity "
            f"pass — promote to live anyway",
            key=ack_key,
        )
    if actions[3].button(
        promote_label,
        key=f"{key_prefix}live_{d.deployment_id}",
        width="stretch",
        disabled=(cur == "live" or not can_promote),
        help=promote_help,
        type=("primary" if parity_passed else "secondary"),
    ):
        dep_mod.update_status(account.login, d.deployment_id, "live")
        # Stamp the deployment notes so the audit trail captures the
        # acknowledged-no-parity override
        if not parity_passed:
            try:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).isoformat(timespec="minutes")
                existing = dep_mod.load_deployments(account.login)
                for dr in existing:
                    if dr.deployment_id == d.deployment_id:
                        note = (
                            f"[PROMOTED-NO-PARITY {now}] User acknowledged "
                            f"missing parity and promoted to live anyway."
                        )
                        dr.notes = (note + (f"\n{dr.notes}"
                                              if dr.notes else ""))
                        break
                dep_mod.save_deployments(account.login, existing)
            except Exception:
                pass
        st.success(
            f"✅  Promoted `{d.deployment_id}` to LIVE. "
            f"View it on the **🟢 Live** page (sidebar). "
            + ("⚠ Note: parity was stale — you overrode the gate."
                 if not parity_passed else "")
        )
        st.rerun()
    if actions[4].button(
        "⛔ Halt", key=f"{key_prefix}halt_{d.deployment_id}",
        width="stretch",
        help="Refuse new signals. Open positions are NOT auto-closed.",
    ):
        dep_mod.update_status(account.login, d.deployment_id, "halted")
        st.rerun()
    if actions[5].button(
        "🗑 Remove", key=f"{key_prefix}rm_{d.deployment_id}",
        width="stretch",
    ):
        dep_mod.remove_deployment(account.login, d.deployment_id)
        st.rerun()


def _render_live_actions(
    account: Any, d: Deployment, *,
    key_prefix: str,
) -> None:
    # 5 columns now: Backtest deep-link first, then the existing 4.
    actions = st.columns(5)
    actions[0].link_button(
        "📊 Backtest",
        url=_backtest_url_for_deployment(d),
        width="stretch",
        help=(
            f"Open Backtest page for {d.strategy} × {d.ticker} × {d.tf} "
            f"with the catalog's exact config."
        ),
    )
    if actions[1].button(
        "⏸ Pause", key=f"{key_prefix}pause_{d.deployment_id}",
        width="stretch",
    ):
        dep_mod.update_status(account.login, d.deployment_id, "paused")
        st.rerun()
    if actions[2].button(
        "📡 Demote → paper",
        key=f"{key_prefix}demote_{d.deployment_id}",
        width="stretch",
    ):
        dep_mod.update_status(account.login, d.deployment_id, "paper")
        try:
            st.toast(f"{d.deployment_id} → paper")
        except Exception:
            pass
        st.rerun()
    if actions[3].button(
        "⛔ HALT", key=f"{key_prefix}halt_{d.deployment_id}",
        width="stretch", type="primary",
        help=("Refuse new signals. Open positions are NOT "
              "auto-closed — manage on Operations page."),
    ):
        dep_mod.update_status(account.login, d.deployment_id, "halted")
        st.rerun()
    if actions[4].button(
        "🗑 Remove", key=f"{key_prefix}rm_{d.deployment_id}",
        width="stretch",
    ):
        dep_mod.remove_deployment(account.login, d.deployment_id)
        st.rerun()


def render_card(
    account: Any,
    deployment: Deployment,
    *,
    mode: Mode,
    parity_passed: Optional[bool] = None,
    base_strategy_name: Optional[str] = None,
    broker_position: Any = None,
    on_parity_request: Optional[Callable[[dict], None]] = None,
    key_prefix: str = "",
) -> None:
    """Render one deployment card with the page-appropriate action set.

    Single source of truth for Paper + Live card rendering. The page
    chooses ``mode`` and provides any mode-specific data; this function
    handles container, status pill, identifier strip, risk%/cap%/$
    widgets, auto-save, action buttons, and notes.

    ``key_prefix`` disambiguates Streamlit widget keys when the same
    deployment is rendered in multiple containers (e.g. tabs).
    """
    with st.container(border=True):
        _render_unified_header(
            account, deployment,
            mode=mode,
            parity_passed=parity_passed,
            broker_position=broker_position,
            base_strategy_name=base_strategy_name,
            on_parity_request=on_parity_request,
            key_prefix=key_prefix,
        )
        if mode == "paper":
            _render_paper_actions(
                account, deployment,
                parity_passed=bool(parity_passed),
                key_prefix=key_prefix,
            )
        elif mode == "live":
            _render_live_actions(
                account, deployment,
                key_prefix=key_prefix,
            )
        # readonly: no action row
        if deployment.notes:
            st.caption(deployment.notes)
