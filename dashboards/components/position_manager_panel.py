"""
position_manager_panel.py — open positions table + Close One / Close All.

Dense layout: a styled DataFrame with R-multiple gradient colouring, a
typed-confirm Close-All button up top, and per-row Close button stamped
inline. INVARIANT-12 (no auto-close phantoms) and INVARIANT-13 (idempotent
reconcile) visible — phantom positions are surfaced with a yellow warning
but never auto-closed.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from core.position_manager import PositionManager


CLOSE_ALL_PHRASE = "CLOSE ALL"


def _r_gradient(r: float | None) -> str:
    """Hex colour string proportional to R-multiple. None / non-numeric → grey."""
    if r is None:
        return "#374151"
    if r >= 1.0:
        return "#15803d"
    if r >= 0:
        return "#166534"
    if r >= -0.5:
        return "#7c2d12"
    return "#7f1d1d"


def _style_pnl(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "color: #9ca3af;"
    if f > 0:
        return "color: #16a34a; font-weight: 600;"
    if f < 0:
        return "color: #dc2626; font-weight: 600;"
    return "color: #9ca3af;"


def render(*, pm: PositionManager, container=None) -> None:
    target = container or st

    # Header strip with title + freshness + refresh button
    from datetime import datetime, timezone

    # Force-refresh wins over cache-staleness on every render. Streamlit
    # reruns this whole component on every interaction so we attach the
    # fetch timestamp to session state — that's what powers the freshness
    # display.
    fetched_at_key = "_pm_fetched_at_utc"
    refresh_key = "_pm_force_refresh_token"
    head = target.columns([3, 1, 1])
    head[0].markdown("### 💼  Position manager")
    if head[2].button("🔄 Refresh now",
                          help="Force a fresh positions_get call against "
                               "the bridge. The displayed positions are "
                               "ALWAYS what the bridge returns this tick — "
                               "we don't cache results across calls.",
                          key="pm_force_refresh",
                          width="stretch"):
        # Bumping the token forces Streamlit to rerun + we re-fetch below
        st.session_state[refresh_key] = (
            st.session_state.get(refresh_key, 0) + 1
        )
        st.session_state[fetched_at_key] = (
            datetime.now(timezone.utc).isoformat()
        )

    try:
        fetch_t0 = datetime.now(timezone.utc)
        positions = pm.list_open()
        fetch_age_ms = int(
            (datetime.now(timezone.utc) - fetch_t0).total_seconds() * 1000
        )
        st.session_state[fetched_at_key] = fetch_t0.isoformat()
        # Render the freshness chip in head[1]
        head[1].markdown(
            f"<div style='text-align:right;font-family:ui-monospace,"
            f"Menlo,monospace;font-size:0.78rem;color:#16a34a;'>"
            f"⚡ live · {fetch_age_ms}ms</div>"
            f"<div style='text-align:right;font-size:0.7rem;color:#6b7280;'>"
            f"fetched {fetch_t0.strftime('%H:%M:%S')} UTC</div>",
            unsafe_allow_html=True,
        )
    except Exception as e:
        msg = str(e)
        if "unknown method" in msg or "not implemented" in msg.lower():
            target.warning(
                "ℹ Your MT5 bridge EA is responding but doesn't implement "
                "the Phase 2.5 position methods (`positions_get`, "
                "`position_close`, `history_deals_get`). Recompile the "
                "EA to pick up the patch — instructions below. "
                "Until then this panel is read-only.")
            with target.expander(
                "🔧  Recompile the bridge EA (~30 seconds)",
                expanded=True,
            ):
                target.markdown(
                    "Your `MT5BridgeFile.mq5` source has already been "
                    "patched on disk with the Phase 2.5 handlers. You "
                    "just need to recompile and reattach:\n\n"
                    "1. **Switch to MetaEditor** "
                    "(it's already open if you've been editing the EA — "
                    "use Cmd+Tab, or in MT5 click *Tools → "
                    "MetaQuotes Language Editor*).\n"
                    "2. Make sure `MT5BridgeFile.mq5` is the active tab "
                    "(version should now read `1.1`).\n"
                    "3. Press **F7** (or click the *Compile* button in "
                    "the toolbar). Output panel should show "
                    "`0 errors, 0 warnings`.\n"
                    "4. Switch back to **MetaTrader 5**.\n"
                    "5. In the *Navigator → Expert Advisors* panel, "
                    "right-click `MT5BridgeFile` → **Refresh** (or "
                    "detach the EA from your chart and re-drag it on).\n"
                    "6. Check the *Experts* log — should show "
                    "`MT5BridgeFile: ready. Watching MQL5/Files/mt5qt/"
                    "req/`.\n"
                    "7. Refresh this dashboard page — the warning is "
                    "gone and the table populates with live positions.\n\n"
                    "Fallback: if recompile fails, install "
                    "[`V2Bridge.mq5`](file://"
                    + str(__file__).rsplit("/dashboards", 1)[0]
                    + "/mql5/V2Bridge.mq5) — full procedure in "
                    "`docs/RUNBOOK.md` § 14."
                )
        else:
            target.error(f"Could not query broker: {msg}")
        return

    # Reconcile to detect manual closes / phantoms (idempotent).
    try:
        report = pm.reconcile_with_broker()
        if report.n_phantom > 0:
            target.warning(
                f"⚠ {report.n_phantom} phantom position(s) on broker that "
                f"we don't track: {sorted(report.phantom_tickets)} — "
                f"NOT auto-closed."
            )
        if report.n_manual_closes_recorded > 0:
            target.info(
                f"📋 Detected {report.n_manual_closes_recorded} manual close"
                f"(s) in MT5 since last refresh — synthesised into journal."
            )
    except Exception as e:
        target.caption(f"(reconcile failed: {e})")

    if not positions:
        target.caption("no open positions")
        return

    n = len(positions)
    unreal = sum(p.unrealized_pnl for p in positions)
    avg_r = (sum((p.unrealized_r or 0.0) for p in positions) / n) if n else 0.0

    # Header row: count + sums + Close-All popover
    head = target.columns([3, 1])
    head[0].markdown(
        f"<div style='font-family:ui-monospace,Menlo,monospace;"
        f"font-size:0.92rem;'>"
        f"<b>{n}</b> open  ·  unrealized "
        f"<b style='color:{'#16a34a' if unreal >= 0 else '#dc2626'};'>"
        f"${unreal:+,.2f}</b>"
        f"  ·  avg R "
        f"<b style='color:{'#16a34a' if avg_r >= 0 else '#dc2626'};'>"
        f"{avg_r:+.2f}</b></div>",
        unsafe_allow_html=True,
    )
    with head[1].popover("⛔  Close ALL", width="stretch"):
        st.markdown(
            f"Type `{CLOSE_ALL_PHRASE}` exactly to confirm closing every "
            f"open position on this account.")
        confirm = st.text_input("confirm", value="",
                                key="pm_closeall_confirm",
                                label_visibility="collapsed")
        if st.button("Close all positions",
                      disabled=(confirm != CLOSE_ALL_PHRASE),
                      type="primary", key="pm_closeall_btn"):
            results = pm.close_all(reason="close_all_ui")
            ok_n = sum(1 for r in results if r.ok)
            st.success(
                f"Closed {ok_n}/{len(results)} positions. "
                f"{len(results) - ok_n} failures.")
            st.rerun()

    # Build table
    rows = []
    for p in positions:
        rows.append({
            "ticket": p.ticket,
            "symbol": p.symbol,
            "side": p.direction.upper(),
            "lots": p.lots,
            "entry": round(p.entry_price, 5),
            "now": round(p.current_price, 5),
            "stop": round(p.stop_price, 5) if p.stop_price else None,
            "target": round(p.target_price, 5) if p.target_price else None,
            "$ pnl": round(p.unrealized_pnl, 2),
            "R": (round(p.unrealized_r, 2)
                  if p.unrealized_r is not None else None),
            "strategy": p.strategy_name or "—",
        })
    df = pd.DataFrame(rows)
    styled = (
        df.style
        .map(_style_pnl, subset=["$ pnl", "R"])
        .format({"entry": "{:.5f}", "now": "{:.5f}",
                 "stop": "{:.5f}", "target": "{:.5f}",
                 "$ pnl": "${:+,.2f}", "R": "{:+.2f}"}, na_rep="—")
    )
    target.dataframe(styled, width="stretch",
                       height=min(420, 36 * (n + 1)))

    # Per-row Close — inline buttons with the position context next to them.
    target.markdown("**Close one:**")
    for p in positions:
        c = target.columns([3, 2, 1, 1])
        c[0].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.85rem;'>"
            f"<code>{p.ticket}</code> · {p.symbol} · "
            f"{p.direction.upper()} {p.lots} lots @ {p.entry_price}"
            f"</span>", unsafe_allow_html=True)
        c[1].markdown(
            f"<span style='font-family:ui-monospace,Menlo,monospace;"
            f"font-size:0.85rem;color:"
            f"{'#16a34a' if p.unrealized_pnl >= 0 else '#dc2626'};'>"
            f"PnL ${p.unrealized_pnl:+,.2f}"
            + (f"  ·  R {p.unrealized_r:+.2f}"
               if p.unrealized_r is not None else "")
            + "</span>", unsafe_allow_html=True)
        if c[2].button("Close", key=f"pm_close_{p.ticket}",
                          type="secondary", width="stretch"):
            res = pm.close_one(p.ticket, reason="manual_close_ui")
            if res.ok:
                st.toast(f"Closed #{p.ticket} (${res.realized_pnl:+,.2f})")
                # Save success result so banner persists across rerun
                st.session_state["_pm_last_close_result"] = {
                    "ok": True,
                    "ticket": res.ticket,
                    "pnl": res.realized_pnl,
                }
            else:
                # FULL error context — not just a one-line toast.
                # Persist to session state so it survives the rerun.
                # Use getattr fallbacks so the panel survives even if the
                # caller (or hot-reloader) is running an OLDER CloseResult
                # without `detailed_error` / `bridge_response` / `attempts`.
                detail = getattr(res, "detailed_error", None)
                if not detail:
                    # Synthesize the same string from primitive fields so the
                    # UI never shows a bare 'failed'.
                    parts = []
                    if getattr(res, "error", ""):
                        parts.append(res.error)
                    rc = getattr(res, "retcode", 0) or 0
                    if rc and rc != 0:
                        parts.append(f"retcode={rc}")
                    detail = " · ".join(parts) if parts else (
                        "close failed (older CloseResult class — restart "
                        "Streamlit to pick up the new error-detail fields)"
                    )
                st.session_state["_pm_last_close_result"] = {
                    "ok": False,
                    "ticket": res.ticket,
                    "detailed_error": detail,
                    "retcode": getattr(res, "retcode", 0),
                    "bridge_response": getattr(res, "bridge_response", {})
                                          or {},
                    "attempts": getattr(res, "attempts", 1),
                }
                st.toast(f"Close FAILED #{p.ticket}: {detail[:80]}",
                            icon="⛔")
            st.rerun()

    # ---- Persistent banner for the most recent close result ----
    last = st.session_state.get("_pm_last_close_result")
    if last is not None:
        target.markdown("---")
        if last["ok"]:
            with target.container(border=True):
                col_a, col_b = st.columns([5, 1])
                col_a.success(
                    f"✅ Closed #{last['ticket']} for "
                    f"${last['pnl']:+,.2f} realized P&L"
                )
                if col_b.button("Dismiss", key="_pm_dismiss_ok"):
                    st.session_state.pop("_pm_last_close_result", None)
                    st.rerun()
        else:
            with target.container(border=True):
                st.error(
                    f"⛔ **Close FAILED for ticket #{last['ticket']}** "
                    f"after {last.get('attempts', 1)} attempt(s)"
                )
                st.markdown(
                    f"**Error:**  `{last['detailed_error']}`"
                )
                # Common retcode → human translation
                rc = last.get("retcode", 0)
                rc_help = {
                    10004: "Requote — broker rejected fill price; will retry.",
                    10006: "Request rejected — likely bad price/lots; check market hours.",
                    10009: "Request completed (this should NOT be ok=False — check bridge).",
                    10013: "Invalid request (params malformed).",
                    10014: "Invalid volume (lots below broker min or above max).",
                    10015: "Invalid price (off-quote — broker not accepting this price).",
                    10016: "Invalid stops (SL/TP too close to current price — broker FREEZE_LEVEL).",
                    10017: "Trade disabled — account may be read-only or symbol disabled.",
                    10018: "Market closed — outside trading hours.",
                    10019: "Not enough money — margin requirements not met.",
                    10020: "Prices changed — requote.",
                    10021: "No quotes — symbol not subscribed or feed dead.",
                    10025: "No changes (when modifying SL/TP — already that value).",
                    10026: "Server disabled autotrading.",
                    10027: "Client (terminal) disabled autotrading — check the AutoTrading button in MT5 toolbar.",
                    10028: "Order locked by another request.",
                    10030: "Unsupported filling mode — try ORDER_FILLING_IOC instead of FOK.",
                    10031: "No connection to trade server.",
                    10038: "Position closed by another order — already gone.",
                }
                if rc in rc_help:
                    st.info(f"Retcode {rc}: {rc_help[rc]}")
                elif rc:
                    st.caption(
                        f"Retcode {rc} not in known list — check MT5 docs at "
                        f"https://www.mql5.com/en/docs/constants/errorswarnings/"
                        f"enum_trade_return_codes for the full table."
                    )
                # Bridge response, raw
                with st.expander("🔍  Raw bridge response (debug)",
                                    expanded=False):
                    st.json(last.get("bridge_response") or {})
                # Action buttons
                bcols = st.columns(3)
                if bcols[0].button("🔄 Retry close",
                                      key="_pm_retry_close",
                                      type="primary",
                                      width="stretch"):
                    res = pm.close_one(last["ticket"],
                                          reason="manual_retry_ui")
                    if res.ok:
                        st.session_state["_pm_last_close_result"] = {
                            "ok": True, "ticket": res.ticket,
                            "pnl": res.realized_pnl,
                        }
                    else:
                        # Same getattr fallback for stale CloseResult
                        retry_detail = getattr(res, "detailed_error", None)
                        if not retry_detail:
                            parts = []
                            if getattr(res, "error", ""):
                                parts.append(res.error)
                            rc = getattr(res, "retcode", 0) or 0
                            if rc:
                                parts.append(f"retcode={rc}")
                            retry_detail = (" · ".join(parts)
                                              or "close failed")
                        st.session_state["_pm_last_close_result"] = {
                            "ok": False, "ticket": res.ticket,
                            "detailed_error": retry_detail,
                            "retcode": getattr(res, "retcode", 0),
                            "bridge_response": getattr(res, "bridge_response",
                                                          {}) or {},
                            "attempts": getattr(res, "attempts", 1),
                        }
                    st.rerun()
                if bcols[1].button("📋 Copy error",
                                      key="_pm_copy_err",
                                      width="stretch",
                                      help="Selects the error text below "
                                            "for copy-paste"):
                    st.code(last["detailed_error"], language="text")
                if bcols[2].button("Dismiss",
                                      key="_pm_dismiss_err",
                                      width="stretch"):
                    st.session_state.pop("_pm_last_close_result", None)
                    st.rerun()
