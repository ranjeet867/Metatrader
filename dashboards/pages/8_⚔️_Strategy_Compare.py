"""
8_⚔️_Strategy_Compare.py — AlgoTest-style strategy versioning + comparison.

Pick N edges from the optimizer leaderboard, see them side-by-side with
the same metrics AlgoTest surfaces in its 'Versions' view:

  • Overall MTM ($)         — total realized P&L on a $100k baseline
  • Avg MTM ($/day)         — per-trading-day return
  • Max DD ($)              — peak-to-trough drawdown
  • R / Max DD              — recovery factor (Overall / Max DD)
  • Win%                    — share of profitable trades
  • DD days                 — how long the worst drawdown lasted
  • P(pass) FTMO            — Monte-Carlo pass rate
  • Pros / Cons             — auto-generated from metrics

Plus a projected-end-balance chart showing where $100k would have ended
under each candidate strategy, side by side.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import deployment as dep_mod               # noqa: E402
from core import edge_catalog                        # noqa: E402
from core.parity_gate import ParityGate              # noqa: E402
from dashboards.components import theme              # noqa: E402
from dashboards.components.strategy_resolver import resolve_base_strategy  # noqa: E402

DB_PATH = REPO / "data" / "v2.db"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _avg_mtm_per_day(net_pnl: float, dd_days_proxy: float,
                      n_test: int) -> float:
    """Approximate avg MTM/day. We don't have the exact span_days here, so
    use n_test × typical-days-per-trade as a rough divisor (with a sane
    floor). Good enough for relative comparison between strategies; the
    detailed Backtest page has the real number."""
    span_days = max(30.0, dd_days_proxy * 1.5, n_test * 2.0)
    return net_pnl / span_days if span_days > 0 else 0.0


def _recovery_factor(net_pnl: float, max_dd_dollars: float) -> float:
    if max_dd_dollars <= 0:
        return float("inf") if net_pnl > 0 else 0.0
    return net_pnl / max_dd_dollars


def _pros_cons(es: edge_catalog.EdgeStat) -> tuple[list[str], list[str]]:
    pros: list[str] = []
    cons: list[str] = []
    if es.win_rate_pct >= 60:
        pros.append(f"High win rate ({es.win_rate_pct:.0f}%) — psychologically easy")
    elif es.win_rate_pct < 40 and es.win_rate_pct > 0:
        cons.append(f"Low win rate ({es.win_rate_pct:.0f}%) — many losers in a row")
    if es.rr_ratio >= 2.0:
        pros.append(f"Strong R:R {es.rr_ratio:.2f} — wins outpay losses ~{es.rr_ratio:.1f}x")
    elif es.rr_ratio and es.rr_ratio < 1.0:
        cons.append(f"R:R only {es.rr_ratio:.2f} — needs high win rate to break even")
    if es.n_test >= 50:
        pros.append(f"Large OOS sample ({es.n_test}) — edge looks real, not fluke")
    elif 0 < es.n_test < 10:
        cons.append(f"Small OOS sample ({es.n_test}) — could be noise")
    if es.max_dd_pct < 3:
        pros.append(f"Tiny drawdown ({es.max_dd_pct:.1f}%) — well below FTMO floor")
    elif es.max_dd_pct > 8:
        cons.append(f"Heavy drawdown ({es.max_dd_pct:.1f}%) — close to FTMO −10%")
    if es.p_pass_30d is not None:
        if es.p_pass_30d >= 0.90:
            pros.append(f"FTMO pass {es.p_pass_30d*100:.0f}% — high challenge survivability")
        elif es.p_pass_30d < 0.30:
            cons.append(f"FTMO pass only {es.p_pass_30d*100:.0f}% — likely to bust")
    rf = _recovery_factor(es.net_pnl_dollars, es.max_dd_dollars)
    if rf >= 3 and rf != float("inf"):
        pros.append(f"Recovery factor {rf:.2f} — profit much larger than worst DD")
    elif rf < 1 and es.net_pnl_dollars > 0:
        cons.append(f"Recovery factor {rf:.2f} — barely outpaces drawdown")
    if es.side == "bidir":
        pros.append("Bidirectional — captures both rallies and reversals")
    if es.max_dd_days > 500:
        cons.append(f"Long underwater period ({es.max_dd_days:.0f} days) — patience needed")
    if es.recovery_days is None and es.max_dd_dollars > 0:
        cons.append("Drawdown not yet recovered — backtest ended underwater from peak")
    return pros, cons


def _to_compare_row(es: edge_catalog.EdgeStat) -> dict:
    rf = _recovery_factor(es.net_pnl_dollars, es.max_dd_dollars)
    avg_mtm = _avg_mtm_per_day(es.net_pnl_dollars, es.max_dd_days, es.n_test)
    return {
        "ticker": es.ticker,
        "tf": es.tf,
        "strategy": es.strategy,
        "side": es.side,
        "R:R": es.rr_label or "—",
        "trades": es.n_test,
        "win%": round(es.win_rate_pct, 1),
        "Overall MTM": round(es.net_pnl_dollars),
        "Avg MTM/day": round(avg_mtm, 1),
        "Max DD": round(es.max_dd_dollars),
        "R/Max DD": ("∞" if rf == float("inf") else round(rf, 2)),
        "DD days": round(es.max_dd_days),
        "P(pass)": (None if es.p_pass_30d is None
                     else f"{es.p_pass_30d*100:.0f}%"),
        "PF": round(es.test_pf, 2),
        "score": round(es.score, 1),
    }


def _projected_balance_chart(rows: list[edge_catalog.EdgeStat],
                               start: float = 100_000.0) -> go.Figure:
    """One bar per strategy showing projected end balance from $100k.

    We only have summary stats (sum_realized + max_dd) per cell, not the
    full equity path — so this is a column chart of end balance, with
    the worst point (start − max_dd) marked.
    """
    fig = go.Figure()
    labels = [f"{r.strategy}<br>{r.ticker} {r.tf} {r.side}" for r in rows]
    end_balances = [start + r.net_pnl_dollars for r in rows]
    worst_balances = [start - r.max_dd_dollars for r in rows]
    colors = ["#16a34a" if e >= start else "#dc2626" for e in end_balances]
    fig.add_trace(go.Bar(
        x=labels, y=end_balances, name="End balance",
        marker_color=colors,
        text=[f"${b:,.0f}" for b in end_balances],
        textposition="outside",
        hovertemplate="%{x}<br>End: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=worst_balances, name="Worst point (peak − maxDD)",
        mode="markers",
        marker=dict(symbol="diamond-open", size=12, color="#fbbf24",
                     line=dict(width=2)),
        hovertemplate="%{x}<br>Worst: $%{y:,.0f}<extra></extra>",
    ))
    fig.add_hline(y=start, line=dict(color="#9ca3af", dash="dash"),
                   annotation_text=f"start ${start:,.0f}",
                   annotation_position="top right",
                   annotation_font=dict(color="#9ca3af", size=10))
    fig.add_hline(y=start * 0.90,
                   line=dict(color="#dc2626", dash="dot"),
                   annotation_text="FTMO −10% floor",
                   annotation_font=dict(color="#dc2626", size=10),
                   annotation_position="bottom right")
    fig.update_layout(
        height=420, margin=dict(l=10, r=10, t=40, b=10),
        title=f"Projected balance from ${start:,.0f}",
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937", tickangle=-30),
        yaxis=dict(gridcolor="#1f2937", title="$"),
        legend=dict(orientation="h", x=0, y=1.10),
    )
    return fig


# ---------------------------------------------------------------------------
# Deploy dialog (parity-aware)
# ---------------------------------------------------------------------------

def _run_parity_for_compare(edge: edge_catalog.EdgeStat,
                              base_name: str) -> tuple[bool, float, str]:
    """Run replay-parity inline for a Strategy-Compare cell.
    Returns (passed, divergence_dollars, message)."""
    import time
    from core.config import load_config
    from core.data import load_parquet
    from core.parity_check import run_parity
    from core.symbol_info_loader import try_load as try_load_symbol_info
    from dashboards.components.state import (
        DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit, discover_strategies,
    )

    strategies = discover_strategies()
    if base_name not in strategies:
        return False, 0.0, f"base `{base_name}` not registered"
    StratCls, ParamsCls = strategies[base_name]
    parquet = REPO / "data" / f"{edge.ticker}_{edge.tf}.parquet"
    if not parquet.exists():
        return False, 0.0, f"no parquet for {edge.ticker}_{edge.tf}"
    cfg = load_config()
    df = load_parquet(parquet)
    try:
        strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    except Exception as e:
        return False, 0.0, f"instantiate failed: {e}"
    sym_info = try_load_symbol_info(edge.ticker)
    t0 = time.time()
    pr = run_parity(
        df, strat, symbol=edge.ticker, tf=edge.tf,
        starting_balance=100_000,
        lots=DEFAULT_LOTS.get(edge.ticker, 0.1),
        money_per_unit_price=resolve_money_per_unit(edge.ticker),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        risk_cfg=cfg, symbol_info=sym_info,
    )
    elapsed = time.time() - t0
    div = pr.divergence_dollars
    if pr.passes:
        ParityGate(DB_PATH).record_pass(base_name, divergence_dollars=div)
        return True, div, (
            f"PASS in {elapsed:.1f}s — div ${div:.6f}, "
            f"{pr.bt.n_trades} trades / ${pr.bt.sum_realized_pnl:+,.2f}"
        )
    return False, div, (
        f"FAIL — div ${div:.4f}: BT {pr.bt.n_trades} / "
        f"${pr.bt.sum_realized_pnl:+,.2f} vs RP {pr.rp.n_trades} / "
        f"${pr.rp.sum_realized_pnl:+,.2f}"
    )


@st.dialog("Deploy candidate", width="large")
def _render_deploy_dialog(edge: edge_catalog.EdgeStat) -> None:
    """Modal: shows summary + parity status + quality gate + risk knob;
    lets user pick paper or live and confirm. Live is gated on BOTH
    fresh parity pass AND quality gate (BLOCK = refused; WARN = allowed
    with explicit acknowledgement)."""
    from core import account_manager
    from core.deployment_quality_gate import (
        evaluate_quality, load_criteria, QualityCriteria,
    )
    from dashboards.components.state import discover_strategies

    strategies = discover_strategies()
    base = resolve_base_strategy(edge.strategy, strategies) or edge.strategy
    gate = ParityGate(DB_PATH)

    # ---- Header ----
    st.markdown(
        f"### `{edge.strategy}` · `{edge.ticker}` `{edge.tf}` · "
        f"{edge.side} · R:R **{edge.rr_label or '—'}**"
    )
    rf = _recovery_factor(edge.net_pnl_dollars, edge.max_dd_dollars)
    kpis = st.columns(5)
    kpis[0].metric("Trades", edge.n_test)
    kpis[1].metric("Win%", f"{edge.win_rate_pct:.1f}%")
    kpis[2].metric("Net P&L", f"${edge.net_pnl_dollars:+,.0f}")
    kpis[3].metric("Max DD", f"${edge.max_dd_dollars:,.0f}",
                       delta=f"-{edge.max_dd_pct:.1f}%",
                       delta_color="inverse")
    kpis[4].metric("R/Max DD",
                       "∞" if rf == float("inf") else f"{rf:.2f}")

    # ---- Quality gate — first defence ----
    qres = evaluate_quality(edge)
    st.markdown("#### Quality gate")
    if qres.is_blocked:
        with st.container(border=True):
            st.error(
                f"⛔ **Quality gate: BLOCK** — "
                f"{len(qres.block_reasons)} failure(s)"
            )
            for reason in qres.block_reasons:
                st.markdown(f"- {reason}")
            st.caption(
                "Live deploy is REFUSED. Type `I UNDERSTAND` below to "
                "force-fall back to paper anyway. Doing this is "
                "strongly discouraged — these specific issues mean the "
                "strategy is mathematically losing or FTMO-incompatible."
            )
    elif qres.verdict == "WARN":
        with st.container(border=True):
            st.warning(
                f"🟡 **Quality gate: WARN** — "
                f"{len(qres.warn_reasons)} concern(s)"
            )
            for reason in qres.warn_reasons:
                st.markdown(f"- {reason}")
    else:
        st.success(
            f"✅ Quality gate: OK ({len(qres.pass_notes)} checks passed)"
        )

    # ---- Account ----
    try:
        accounts = account_manager.list_accounts()
    except Exception:
        accounts = []
    if not accounts:
        st.error("⛔ No MT5 account configured. Configure one on Account & "
                  "Risk before deploying.")
        if st.button("Close", width="stretch"):
            st.session_state.pop("_sc_deploy_edge", None)
            st.rerun()
        return
    acct_labels = [f"{a.alias} (#{a.login})" for a in accounts]
    sel_idx = st.selectbox("Account", list(range(len(accounts))),
                              format_func=lambda i: acct_labels[i],
                              key="_sc_deploy_acct")
    account = accounts[sel_idx]
    equity = float(account.effective_baseline_equity)

    # ---- Parity status + inline run ----
    st.markdown("#### Replay-parity gate")
    parity_ok = gate.is_recent(base)
    last = gate.last_pass_for(base)
    if parity_ok:
        ts = last[0].strftime("%Y-%m-%d %H:%M") if last else ""
        div = last[1] if last else 0.0
        st.success(
            f"✅ Parity is fresh for `{base}` "
            f"(last pass {ts} UTC, divergence ${div:.6f}). "
            f"Live deployment is unlocked."
        )
    else:
        st.error(
            f"⛔ No recent replay-parity pass for `{base}`. Live "
            f"deployment is BLOCKED — run the parity check below or "
            f"deploy as paper instead."
        )

    # Always offer Run-parity-now so the user can clear the gate without
    # leaving the dialog
    parity_result = st.session_state.get("_sc_parity_result")
    pcol = st.columns([1, 3])
    if pcol[0].button("🔬 Run parity now", type="primary",
                        width="stretch",
                        key="_sc_run_parity"):
        with st.spinner(f"Running parity for {base}..."):
            ok, div, msg = _run_parity_for_compare(edge, base)
        st.session_state["_sc_parity_result"] = {
            "ok": ok, "div": div, "msg": msg,
        }
        st.rerun()
    if parity_result is not None:
        if parity_result["ok"]:
            pcol[1].success(f"✅ {parity_result['msg']}")
        else:
            pcol[1].error(f"⛔ {parity_result['msg']}")

    # ---- Sizing ----
    st.markdown("#### Sizing & limits")
    sz = st.columns(3)
    risk_pct = float(sz[0].number_input(
        "Risk per trade (%)", value=0.30,
        min_value=0.05, max_value=2.0, step=0.05, format="%.2f",
        key="_sc_risk_pct",
        help="$/trade = equity × risk%. Stops are derived from "
              "ATR/structure — this only sizes the position.",
    ))
    daily_cap_pct = float(sz[1].number_input(
        "Daily cap (%)", value=2.5,
        min_value=0.5, max_value=5.0, step=0.5, format="%.1f",
        key="_sc_daily_cap_pct",
        help="Halts new entries once today's realised loss "
              "exceeds this fraction of equity.",
    ))
    sz[2].metric("$/trade", f"${equity * risk_pct / 100.0:,.0f}")

    # ---- Action buttons ----
    st.markdown("#### Confirm")
    # Override input — only matters when quality is BLOCK
    override_token = ""
    if qres.is_blocked:
        override_token = st.text_input(
            "Quality override (type `I UNDERSTAND` to bypass — paper only)",
            value="", key="_sc_quality_override",
            placeholder="leave blank to respect the BLOCK",
        )

    btns = st.columns([1, 1, 1])

    def _do_deploy(status: str) -> None:
        # Quality gate FIRST — strict for live, lenient for paper.
        # Live BLOCK is final unless override token is supplied.
        # Paper accepts the lenient threshold automatically.
        if status == "live":
            live_qres = evaluate_quality(edge,
                                            criteria=load_criteria(account.login))
            if live_qres.is_blocked and override_token != "I UNDERSTAND":
                st.toast(
                    f"⛔ Live BLOCKED by quality gate ("
                    f"{len(live_qres.block_reasons)} failure(s))",
                    icon="⛔",
                )
                return
        else:
            paper_qres = evaluate_quality(edge,
                                              criteria=QualityCriteria.lenient())
            if paper_qres.is_blocked and override_token != "I UNDERSTAND":
                st.toast(
                    f"⛔ Paper BLOCKED — even lenient gate refused "
                    f"({len(paper_qres.block_reasons)} failure(s)). "
                    f"Type override token to force.",
                    icon="⛔",
                )
                return
        # Parity gate next — only blocks live
        if status == "live" and not gate.is_recent(base):
            st.toast("⛔ Live blocked — no recent parity for "
                      f"`{base}`. Run parity above or pick paper.",
                      icon="⛔")
            return
        # If overriding a BLOCK, force paper for safety
        actual = status
        if qres.is_blocked and override_token == "I UNDERSTAND":
            actual = "paper"
            st.toast(
                "⚠ Quality BLOCK overridden — forcing paper for safety",
                icon="⚠",
            )
        dep_id = (dep_mod.Deployment.slug(edge.strategy, edge.ticker, edge.tf)
                   + ("_long" if edge.side == "long" else "_bidir"))
        d = dep_mod.Deployment(
            deployment_id=dep_id,
            strategy=edge.strategy,
            ticker=edge.ticker, tf=edge.tf,
            long_only=(edge.side == "long"),
            params={"long_only": (edge.side == "long")},
            risk_pct=risk_pct,
            daily_cap_pct=daily_cap_pct,
            status=actual,
            notes=(f"Strategy Compare: P(pass) "
                    f"{(edge.p_pass_30d or 0)*100:.0f}%, "
                    f"R:R {edge.rr_label}, win% {edge.win_rate_pct:.0f}"
                    + (" [QUALITY-OVERRIDE]"
                       if qres.is_blocked and override_token == "I UNDERSTAND"
                       else "")),
        )
        dep_mod.upsert_deployment(account.login, d)
        st.session_state.pop("_sc_deploy_edge", None)
        st.session_state.pop("_sc_parity_result", None)
        st.toast(f"✅ Deployed `{edge.strategy}` to {actual.upper()}")
        st.rerun()

    # Disable buttons when gates fail (unless override)
    paper_blocked = (qres.is_blocked and override_token != "I UNDERSTAND")
    live_blocked = (paper_blocked
                       or not gate.is_recent(base)
                       or qres.is_blocked)
    if btns[0].button(
        "🟡 Deploy to paper", type="secondary",
        width="stretch",
        key="_sc_btn_paper",
        disabled=paper_blocked,
        help=("Quality gate BLOCK — type override to bypass"
              if paper_blocked else None),
    ):
        _do_deploy("paper")
    if btns[1].button(
        "🟢 Deploy to live", type="primary",
        width="stretch",
        disabled=live_blocked,
        key="_sc_btn_live",
        help=("Pass replay-parity AND quality gate to unlock"
              if live_blocked else None),
    ):
        _do_deploy("live")
    if btns[2].button("Cancel", width="stretch",
                        key="_sc_btn_cancel"):
        st.session_state.pop("_sc_deploy_edge", None)
        st.session_state.pop("_sc_parity_result", None)
        st.rerun()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Strategy Compare", page_icon="⚔️",
                        layout="wide")
    theme.inject_css()
    st.title("⚔️  Strategy Compare")
    st.caption(
        "AlgoTest-style side-by-side: pick N candidate edges, compare on "
        "the same parameters, see which would actually keep you alive on "
        "the FTMO challenge. All numbers on a $100k baseline.")

    cat = edge_catalog.load_catalog()
    if not cat:
        st.warning("No edge data. Run `make optimize` first.")
        return

    # Flatten the catalog
    all_edges: list[edge_catalog.EdgeStat] = []
    for rows in cat.values():
        all_edges.extend(rows)
    # Only keep cells that have $-data (filters out grid-only fallbacks)
    rich = [e for e in all_edges if e.net_pnl_dollars != 0
             or e.max_dd_dollars != 0]
    if not rich:
        st.warning(
            "No optimizer-grade rows yet (every cell has $0 net P&L). "
            "Re-run `make optimize` so the wide-format markdown is fresh.")
        return
    # Sort by score descending
    rich.sort(key=lambda e: -e.score)

    # ── Filters (Phase-32: moved from sidebar to top of main) ─────────
    # Sidebar reserved for navigation only (matches Backtest / Composer
    # / Replay Parity pattern). Filters live in a collapsible expander
    # so the page is uncluttered when defaults are fine, but power-user
    # knobs are one click away.
    with st.expander("🔎  Filter candidates", expanded=False):
        f_cols = st.columns([1, 1, 1, 1])
        with f_cols[0]:
            only_survivors = st.checkbox(
                "Survivors only (test_R > 0, PF > 1)",
                value=True,
            )
            only_deploy_safe = st.checkbox(
                "Deploy-safe only (passes hard gates)",
                value=True,
                help="Hides cells that would be rejected by the "
                      "Composer's hard gates and the deploy quality "
                      "gate. Untick to see ALL cells including risky "
                      "ones.",
            )
        with f_cols[1]:
            side_filter = st.multiselect(
                "Side",
                options=["long", "bidir"],
                default=["long", "bidir"],
            )
        with f_cols[2]:
            tf_filter = st.multiselect(
                "Timeframe",
                options=sorted({e.tf for e in rich}),
                default=sorted({e.tf for e in rich}),
            )
        with f_cols[3]:
            ticker_filter = st.multiselect(
                "Ticker",
                options=sorted({e.ticker for e in rich}),
                default=[],
                help="Empty = all tickers",
            )
        n_top = st.slider("How many top candidates?", 2, 20, 8)

    # Apply filters
    filtered = list(rich)
    if only_survivors:
        filtered = [e for e in filtered if e.is_survivor]
    if only_deploy_safe:
        # Defensive — older catalog rows may not have the .deploy_safe
        # property if the EdgeStat dataclass was reloaded mid-session.
        filtered = [e for e in filtered
                     if getattr(e, "deploy_safe", True)]
    if side_filter:
        filtered = [e for e in filtered if e.side in side_filter]
    if tf_filter:
        filtered = [e for e in filtered if e.tf in tf_filter]
    if ticker_filter:
        filtered = [e for e in filtered if e.ticker in ticker_filter]

    if not filtered:
        st.warning(
            "No cells match the filters. Open the **🔎 Filter "
            "candidates** expander above and untick 'Deploy-safe "
            "only' or 'Survivors only'."
        )
        return

    top_n = filtered[:n_top]
    if only_deploy_safe:
        st.success(
            f"✅ {len(top_n)} cells shown — all pass the hard gates "
            f"(overall PF ≥ 1.05, TEST PF ≥ 1.0, TEST avg_R ≥ 0, "
            f"recovery ≤ 90d, n_test ≥ 15). Untick 'Deploy-safe only' "
            f"in the **🔎 Filter candidates** expander above to see "
            f"all candidates including ones that would block at deploy.",
            icon="🛡️",
        )

    # ── Comparison table ──────────────────────────────────────────────
    st.markdown(f"### Top {len(top_n)} candidates")
    # Cost-config provenance — same one used by the catalog, so the
    # numbers below are directly comparable to Composer / Library /
    # Backtest. Single source of truth = core.cost_defaults.
    from core import cost_defaults as _cd
    st.caption(
        f"⚖️ **Benchmark** — {_cd.cost_config_badge()}. Click any row's "
        f"📊 Backtest link to open that cell with the EXACT config that "
        f"produced these metrics — fresh run will reproduce these numbers."
    )
    rows = [_to_compare_row(e) for e in top_n]
    df = pd.DataFrame(rows)
    # Phase-32: Edge Score column + shared explainer (same component
    # used on Composer / Library / Backtest)
    from core import edge_score as _es
    from dashboards.components import edge_score_explainer
    df["Edge Score"] = [
        round(_es.compute_from_edge_stat(e).total, 1) for e in top_n
    ]
    edge_score_explainer.render_explainer(expanded=False)
    # Add per-row Backtest deep-link (full config embedded → fresh
    # Backtest run reproduces these exact metrics)
    from dashboards.components.backtest_link import build_backtest_url
    df["📊 Backtest"] = [build_backtest_url(e) for e in top_n]
    # Format dollar columns
    fmt = {
        "Overall MTM": "${:+,.0f}",
        "Avg MTM/day": "${:+,.1f}",
        "Max DD": "${:,.0f}",
    }
    # NOTE: Streamlit's column_config (LinkColumn) requires a non-styled
    # DataFrame — so when we want clickable links we use st.dataframe
    # with column_config instead of the .style.format path. We keep
    # the colour styling in the *unlinked* pros/cons section below.
    st.dataframe(
        df, width="stretch",
        height=min(560, 36 * (len(df) + 1)),
        column_config={
            "📊 Backtest": st.column_config.LinkColumn(
                "📊 Backtest",
                display_text="Open",
                help="Open this cell in the Backtest page with the "
                      "exact config that produced these metrics. "
                      "Fresh run will reproduce these numbers.",
            ),
            "Edge Score": st.column_config.NumberColumn(
                "Edge Score", format="%.1f",
                help=edge_score_explainer.COLUMN_HEADER_HELP,
            ),
            "Overall MTM": st.column_config.NumberColumn(
                format="$%+,.0f"),
            "Avg MTM/day": st.column_config.NumberColumn(
                format="$%+,.1f"),
            "Max DD": st.column_config.NumberColumn(format="$%,.0f"),
        },
    )

    # ── Projected end-balance chart ───────────────────────────────────
    st.plotly_chart(_projected_balance_chart(top_n),
                     width="stretch")

    # ── Pros / Cons per strategy ──────────────────────────────────────
    st.markdown("### Pros / Cons per candidate")
    for i, e in enumerate(top_n, 1):
        pros, cons = _pros_cons(e)
        with st.expander(
            f"**{i}.**  `{e.strategy}` · {e.ticker} {e.tf} · "
            f"{e.side} · {e.rr_label or '—'}  ·  score {e.score:.1f}",
            expanded=(i <= 3),
        ):
            cols = st.columns(2)
            with cols[0]:
                st.markdown("**✅ Pros**")
                if pros:
                    for p in pros:
                        st.markdown(f"- {p}")
                else:
                    st.markdown("_(none flagged)_")
            with cols[1]:
                st.markdown("**⚠ Cons**")
                if cons:
                    for c in cons:
                        st.markdown(f"- {c}")
                else:
                    st.markdown("_(none flagged)_")
            # Mini KPI strip
            rf = _recovery_factor(e.net_pnl_dollars, e.max_dd_dollars)
            kpis = st.columns(6)
            kpis[0].metric("Trades", e.n_test)
            kpis[1].metric("Win%", f"{e.win_rate_pct:.1f}%")
            kpis[2].metric("Net P&L", f"${e.net_pnl_dollars:+,.0f}")
            kpis[3].metric("Max DD", f"${e.max_dd_dollars:,.0f}",
                              delta=f"-{e.max_dd_pct:.1f}%",
                              delta_color="inverse")
            kpis[4].metric("R/Max DD",
                              "∞" if rf == float("inf") else f"{rf:.2f}")
            kpis[5].metric("FTMO pass",
                              "—" if e.p_pass_30d is None
                              else f"{e.p_pass_30d*100:.0f}%")

            # ---- Action row: Backtest deep-link + Deploy modal ------
            # Side-by-side so the user can verify metrics before going
            # straight to Deploy. The Backtest link opens with the EXACT
            # config that produced these compare-page numbers.
            edge_key = f"{e.strategy}__{e.ticker}__{e.tf}__{e.side}"
            from dashboards.components.backtest_link import (
                build_backtest_url as _bt_url,
            )
            act_cols = st.columns([2, 3])
            act_cols[0].link_button(
                "📊 Open in Backtest",
                _bt_url(e),
                width="stretch",
                help="Reproduce these metrics with the exact stored "
                      "config (stop_atr_mult, target_atr_mult, etc).",
            )
            if act_cols[1].button(
                "🚀 Deploy this strategy", key=f"_sc_deploy_{i}",
                type="primary", width="stretch",
            ):
                st.session_state["_sc_deploy_edge"] = edge_key
                # Reset stale parity result from a previous open
                st.session_state.pop("_sc_parity_result", None)
                st.rerun()

    # ── Render deploy dialog for the clicked candidate ────────────────
    pending = st.session_state.get("_sc_deploy_edge")
    if pending is not None:
        # Resolve back to the EdgeStat — we keep the key in session
        # so reruns don't lose the row.
        match = next(
            (e for e in top_n
             if f"{e.strategy}__{e.ticker}__{e.tf}__{e.side}" == pending),
            None,
        )
        if match is not None:
            _render_deploy_dialog(match)
        else:
            st.session_state.pop("_sc_deploy_edge", None)

    st.markdown("---")
    st.caption(
        "💡 **R/Max DD** (recovery factor) is the key headline: ≥3 means "
        "the strategy made >3× its worst drawdown. **Avg MTM/day** is "
        "approximate (uses sample-based span); for the exact daily P&L "
        "curve open the Backtest page for any single cell. The "
        "**🚀 Deploy** button on each row opens a confirmation modal "
        "with an inline replay-parity check before deploying to "
        "paper / live."
    )


main()
