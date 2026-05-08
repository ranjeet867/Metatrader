"""
4_📈_Performance.py — combined trade journal across backtest / paper / live
+ R-distribution / drawdown / monthly heatmap / editable notes.

Pulls trades from data/v2.db (mode in {backtest, paper, live}). Filters by
time range, mode, strategy, ticker, close_reason.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import storage   # noqa: E402


@st.cache_data(ttl=10)
def _load_runs(db_path: str) -> pd.DataFrame:
    """Each row = one backtest / paper / live run with its config + outcome."""
    if not Path(db_path).exists():
        return pd.DataFrame()
    with storage.connect(db_path) as c:
        try:
            rows = c.execute("""
                SELECT run_id, started_at_utc, finished_at_utc,
                       symbol, tf, strategy_name, config_json,
                       starting_balance, ending_equity, n_trades,
                       sum_realized_pnl, equity_curve_pnl, reconciles
                FROM runs ORDER BY started_at_utc DESC
            """).fetchall()
        except Exception:
            return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=[
        "run_id", "started_at_utc", "finished_at_utc",
        "symbol", "tf", "strategy", "config_json",
        "starting_balance", "ending_equity", "n_trades",
        "sum_realized_pnl", "equity_curve_pnl", "reconciles",
    ])


def _run_label(row: pd.Series) -> str:
    """Display label for a run row."""
    when = str(row.get("started_at_utc", ""))[:16]
    return (f"{row['strategy']} · {row['symbol']} · {row['tf']} · "
            f"{row['n_trades']} trades · ${row['sum_realized_pnl']:+,.0f} · "
            f"{when}")


@st.cache_data(ttl=10)
def _load_trades(db_path: str) -> pd.DataFrame:
    if not Path(db_path).exists():
        return pd.DataFrame()
    with storage.connect(db_path) as c:
        rows = c.execute("""
            SELECT t.run_id, t.trade_idx, t.symbol, t.direction,
                   t.opened_at_utc, t.closed_at_utc,
                   t.entry_price, t.exit_price, t.lots,
                   t.realized_pnl, t.r_multiple, t.close_reason,
                   t.mode, t.strategy, t.tf, n.note
            FROM trades t
            LEFT JOIN trade_notes n ON n.trade_run_id = t.run_id
                                    AND n.trade_idx = t.trade_idx
            WHERE t.realized_pnl IS NOT NULL
            ORDER BY t.closed_at_utc
        """).fetchall()
    cols = ["run_id", "trade_idx", "symbol", "direction",
             "opened_at_utc", "closed_at_utc", "entry", "exit", "lots",
             "pnl", "R", "close_reason", "mode", "strategy", "tf", "note"]
    df = pd.DataFrame(rows, columns=cols)
    if not df.empty:
        df["closed_at_utc"] = pd.to_datetime(df["closed_at_utc"], utc=True,
                                                errors="coerce")
        df["opened_at_utc"] = pd.to_datetime(df["opened_at_utc"], utc=True,
                                                errors="coerce")
    return df


def render_filters(df: pd.DataFrame, runs: pd.DataFrame) -> pd.DataFrame:
    """Render filters in a top-of-page expander (Phase-32: moved from
    sidebar to main content for consistency with Backtest / Composer /
    Compare / Replay Parity). Sidebar reserved for navigation only.

    Default scopes to the most-recent single run — the trade journal
    accumulates every backtest, so summing all of them mixes apples
    and oranges (different starting balances, lot sizes, strategies).
    """
    if df.empty:
        return df

    with st.expander("🔎  Filters (scope + attributes)", expanded=False):
        # ── Run scope picker ──
        st.markdown("**Scope**")
        scope_options = ["Single run (most recent)", "Compare runs",
                          "All trades (raw journal)"]
        scope = st.radio("scope", scope_options, index=0,
                          key="perf_scope",
                          label_visibility="collapsed",
                          horizontal=True)

        f = df.copy()
        if scope == "Single run (most recent)":
            if not runs.empty:
                run_labels = {r["run_id"]: _run_label(r)
                               for _, r in runs.iterrows()}
                chosen = st.selectbox(
                    "run", options=list(run_labels.keys()),
                    format_func=lambda rid: run_labels[rid],
                    index=0, key="perf_single_run",
                )
                f = f[f["run_id"] == chosen]
        elif scope == "Compare runs":
            if not runs.empty:
                run_labels = {r["run_id"]: _run_label(r)
                               for _, r in runs.iterrows()}
                chosen = st.multiselect(
                    "runs", options=list(run_labels.keys()),
                    format_func=lambda rid: run_labels[rid],
                    default=list(run_labels.keys())[:3],
                    key="perf_multi_run",
                )
                if chosen:
                    f = f[f["run_id"].isin(chosen)]
        # else: scope == "All trades" → no run filter

        # Attribute filters in 4-column grid
        st.markdown("**Attributes**")
        attr_cols = st.columns(4)
        with attr_cols[0]:
            modes = sorted(f["mode"].dropna().unique().tolist())
            sel_modes = st.multiselect(
                "mode", modes, default=modes, key="perf_modes",
            )
        with attr_cols[1]:
            strats = sorted(f["strategy"].dropna().unique().tolist())
            sel_strats = st.multiselect(
                "strategy", strats, default=strats, key="perf_strats",
            )
        with attr_cols[2]:
            syms = sorted(f["symbol"].dropna().unique().tolist())
            sel_syms = st.multiselect(
                "symbol", syms, default=syms, key="perf_syms",
            )
        with attr_cols[3]:
            reasons = sorted(f["close_reason"].dropna().unique().tolist())
            sel_reasons = st.multiselect(
                "close_reason", reasons, default=reasons,
                key="perf_reasons",
            )
        if sel_modes:
            f = f[f["mode"].isin(sel_modes)]
        if sel_strats:
            f = f[f["strategy"].isin(sel_strats)]
        if sel_syms:
            f = f[f["symbol"].isin(sel_syms)]
        if sel_reasons:
            f = f[f["close_reason"].isin(sel_reasons)]

        # Persist scope label for the explainer line
        st.session_state["_perf_scope_label"] = scope
    return f.reset_index(drop=True)


def _basis_caption(df: pd.DataFrame) -> str:
    """Build a one-line caption describing what mix of trades this
    metric block is computed from. Pre-fix every tile said its number
    without explaining whether it was live, paper, or backtest data —
    user couldn't tell what the basis was."""
    if df.empty:
        return "(no trades match filters)"
    by_mode = df["mode"].value_counts()
    parts = []
    for m in ("live", "paper", "backtest"):
        n = int(by_mode.get(m, 0))
        if n > 0:
            parts.append(f"{n} {m}")
    return "Computed from: " + " · ".join(parts) + (
        f" trades over " +
        f"{df['closed_at_utc'].min().strftime('%Y-%m-%d')} → "
        f"{df['closed_at_utc'].max().strftime('%Y-%m-%d')}"
        if pd.notna(df['closed_at_utc']).any() else ""
    )


def render_metrics(df: pd.DataFrame):
    n = len(df)
    if n == 0:
        st.info(
            "No trades match these filters. Default scope is the most "
            "recent run — try widening the date range or changing the "
            "mode filter above. If you have NO live/paper trades yet, "
            "scroll to **🔮 Expected stats** below for catalog projections."
        )
        return
    pnl = float(df["pnl"].sum())
    wins = (df["pnl"] > 0).sum()
    win_rate = wins / n * 100
    gw = df.loc[df["pnl"] > 0, "pnl"].sum()
    gl = -df.loc[df["pnl"] <= 0, "pnl"].sum()
    pf = gw / gl if gl > 0 else float("inf") if gw > 0 else 0
    avg_R = df["R"].mean()
    # ── Source-labeled metrics (Phase-32) ─────────────────────────────
    # User's complaint: "performance page chart still i can not
    # understand what data it show". Fix: every tile now has a basis
    # caption so it's never ambiguous what produced the number.
    st.markdown("### 📊  Realized P&L metrics")
    st.caption(_basis_caption(df))
    cols = st.columns(5)
    cols[0].metric(
        "Trades", n,
        help="Total closed trades in the filtered set. "
              "Excludes open positions (those have unrealized P&L only).",
    )
    cols[1].metric(
        "Win rate", f"{win_rate:.1f}%",
        help="(wins / total) × 100. A coin flip is 50%. RSI mean-rev "
              "typically 55-65%; trend-following 35-45% with R:R > 1.",
    )
    pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
    cols[2].metric(
        "Profit factor", pf_text,
        help="Gross wins ÷ gross losses. >1.0 profitable, >1.5 strong, "
              "<1.0 losing. Be skeptical of PF >5 on <30 trades — "
              "small sample noise.",
    )
    cols[3].metric(
        "Avg R", f"{avg_R:+.3f}",
        help="Mean R-multiple per trade. R = (exit − entry) / "
              "(entry − stop). +0.20R per trade is a real edge; "
              "+0.50R is excellent.",
    )
    cols[4].metric(
        "Net P&L (realized)", f"${pnl:+,.2f}",
        help="Sum of realized_pnl across all trades in the filtered set. "
              "Positive = winning overall.",
    )


def render_equity(df: pd.DataFrame, key_suffix: str = ""):
    if df.empty:
        return
    df_s = df.sort_values("closed_at_utc").copy()
    df_s["cum_pnl"] = df_s["pnl"].cumsum()
    st.markdown("### 📈  Cumulative P&L curve")
    st.caption(
        "Each point on the green line = closed trade. y-axis is total "
        "$ profit since the first trade in the filtered set. Going up "
        "and to the right = winning. Flat or down = losing. "
        "**Hover** any point to see the exact trade. " +
        _basis_caption(df)
    )
    fig = go.Figure(go.Scatter(
        x=df_s["closed_at_utc"], y=df_s["cum_pnl"],
        mode="lines+markers", line=dict(color="#0a4", width=1.5),
        marker=dict(size=4),
        hovertemplate=(
            "<b>%{x|%Y-%m-%d %H:%M}</b><br>"
            "cum P&L: $%{y:,.2f}<extra></extra>"
        ),
    ))
    fig.update_layout(
        xaxis_title="trade close time", yaxis_title="$ cumulative",
        height=320, margin=dict(l=10, r=10, t=30, b=10),
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch",
                    key=f"perf_equity_{key_suffix}")
    # Drawdown
    peak = df_s["cum_pnl"].cummax()
    dd = df_s["cum_pnl"] - peak
    max_dd = float(dd.min()) if len(dd) > 0 else 0.0
    st.markdown("### 📉  Drawdown curve")
    st.caption(
        f"Distance below the running peak — i.e. how much you were "
        f"underwater at any moment. **Worst drawdown in this set: "
        f"${max_dd:,.0f}**. The closer the red line stays to zero, "
        f"the smoother the equity curve. Long flat-at-zero stretches "
        f"= no drawdown (we're at the all-time high)."
    )
    fig2 = go.Figure(go.Scatter(
        x=df_s["closed_at_utc"], y=dd, mode="lines",
        line=dict(color="#c33", width=0.8), fill="tozeroy",
        fillcolor="rgba(204,51,51,0.25)",
        hovertemplate=(
            "<b>%{x|%Y-%m-%d %H:%M}</b><br>"
            "drawdown: $%{y:,.2f}<extra></extra>"
        ),
    ))
    fig2.update_layout(
        xaxis_title="trade close time", yaxis_title="$ underwater",
        height=180, margin=dict(l=10, r=10, t=30, b=10),
    )
    st.plotly_chart(fig2, width="stretch",
                    key=f"perf_drawdown_{key_suffix}")


def render_r_distribution(df: pd.DataFrame, key_suffix: str = ""):
    if df.empty:
        return
    st.markdown("**🎯 R-multiple distribution**")
    st.caption(
        "How each trade ended in **R-multiples** — units of risk. "
        "1R win = exit price hit the take-profit (= 1× the planned "
        "stop distance). −1R = stop hit. Trades cluster near +1R / −1R "
        "for fixed-target strategies; spread out for trend-followers. "
        "**Right of zero = winning trades**, left = losing. A healthy "
        "edge has a fat right tail or sparse big-loss left tail."
    )
    fig = go.Figure(go.Histogram(
        x=df["R"], nbinsx=40, marker_color="#3a8ac4",
        hovertemplate="R bin: %{x}<br>trades: %{y}<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color="#888", width=1, dash="dot"))
    fig.update_layout(
        xaxis_title="R-multiple per trade",
        yaxis_title="number of trades",
        height=300, margin=dict(l=10, r=10, t=10, b=10),
    )
    st.plotly_chart(fig, width="stretch",
                    key=f"perf_rdist_{key_suffix}")


def render_monthly_heatmap(df: pd.DataFrame, key_suffix: str = ""):
    if df.empty or df["closed_at_utc"].isna().all():
        return
    df = df.copy()
    df["year"] = df["closed_at_utc"].dt.year
    df["month"] = df["closed_at_utc"].dt.month
    monthly = df.groupby(["year", "month"])["pnl"].sum().reset_index()
    pivot = monthly.pivot(index="year", columns="month", values="pnl")
    if pivot.empty:
        return
    st.markdown("### 🗓️  Monthly P&L heatmap (year × month)")
    st.caption(
        "Each cell = sum of realized P&L for that calendar month. "
        "Green = profitable month, red = losing. Empty cells = no "
        "trades. Useful for spotting **regime breaks**: if the last "
        "3 months turn red while earlier months were green, the "
        "strategy may have decayed and need re-eval."
    )
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorscale="RdYlGn", zmid=0,
        text=[[f"${v:+,.0f}" if pd.notna(v) else "" for v in row]
               for row in pivot.values],
        texttemplate="%{text}", textfont=dict(size=10),
        colorbar=dict(title="$ P&L"),
        hovertemplate="<b>%{y}-%{x}</b><br>$%{z:+,.0f}<extra></extra>",
    ))
    fig.update_layout(
        xaxis_title="month (1=Jan, 12=Dec)", yaxis_title="year",
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(tickmode="linear", tick0=1, dtick=1),
    )
    st.plotly_chart(fig, width="stretch",
                    key=f"perf_heatmap_{key_suffix}")


def render_attribution_pie(df: pd.DataFrame, key_suffix: str = ""):
    if df.empty:
        return
    st.markdown("**🥧 P&L attribution by strategy**")
    st.caption(
        "How much of the total profit came from each strategy. Useful "
        "when you have multiple cells live: tells you which is "
        "actually carrying the account vs which is dead weight. A "
        "single dominant slice + 4 tiny slivers = you're really "
        "running 1 strategy plus noise."
    )
    by_strat = df.groupby("strategy")["pnl"].sum().sort_values(ascending=False)
    fig = go.Figure(go.Pie(
        labels=by_strat.index.tolist(),
        values=by_strat.values.tolist(),
        hole=0.45,
        hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<br>"
                       "%{percent}<extra></extra>",
    ))
    fig.update_layout(
        height=320, margin=dict(l=10, r=10, t=10, b=10),
        showlegend=True,
    )
    st.plotly_chart(fig, width="stretch",
                    key=f"perf_attr_{key_suffix}")


def render_journal_table(df: pd.DataFrame, db_path: str,
                          key_suffix: str = ""):
    if df.empty:
        return
    st.markdown("---")
    st.markdown("**Trade journal** — edit `note` cells; press Enter to save.")
    edited = st.data_editor(
        df[["closed_at_utc", "mode", "strategy", "symbol", "tf",
            "direction", "entry", "exit", "lots", "pnl", "R",
            "close_reason", "note", "run_id", "trade_idx"]],
        width="stretch", height=480,
        disabled=["closed_at_utc", "mode", "strategy", "symbol", "tf",
                  "direction", "entry", "exit", "lots", "pnl", "R",
                  "close_reason", "run_id", "trade_idx"],
        key=f"perf_journal_editor_{key_suffix}",
    )
    # Persist any note changes
    changed = (edited["note"].fillna("") != df["note"].fillna(""))
    if changed.any():
        now = datetime.now(timezone.utc).isoformat()
        for _, row in edited[changed].iterrows():
            storage.upsert_trade_note(db_path, row["run_id"], row["trade_idx"],
                                       row["note"] or "", now)
        st.toast(f"Saved {changed.sum()} note(s)", icon="💾")


def render_basis_explainer(df: pd.DataFrame,
                             runs: pd.DataFrame | None) -> None:
    """Tell the operator exactly where these numbers come from.

    Source of confusion this fixes: the journal accumulates EVERY
    backtest / paper / live run you've ever done. Showing the unfiltered
    union conflates strategies, tickers, lot sizes, R:R configs — a
    big +$155k cumulative number is meaningless because it adds apples
    to oranges. The default scope is now 'single run' so the chart
    reflects ONE coherent strategy/portfolio."""
    n = len(df)
    n_runs = (len(runs) if runs is not None and not runs.empty else 0)
    if n == 0 and n_runs == 0:
        st.info(
            "**No trades yet.** This page reads from the SQLite trade "
            "journal (`data/v2.db` and per-account databases). Once "
            "you click 📊 Backtest, 📡 Paper, or 🚀 Go Live, the "
            "trades land here automatically.\n\n"
            "Until then, see the **🏛️ Strategy Library** page for the "
            "expected stats from the latest grid sweep.")
        return
    scope = st.session_state.get("_perf_scope_label",
                                   "Single run (most recent)")
    modes = ", ".join(sorted(df["mode"].dropna().unique().tolist())) or "—"
    syms = ", ".join(sorted(df["symbol"].dropna().unique().tolist())) or "—"
    st.caption(
        f"**Scope:** {scope}  ·  **{n} trades** "
        f"from {n_runs} total runs in `data/v2.db`.  "
        f"Modes: {modes}. Symbols: {syms}.  "
        f"Switch scope in the sidebar to inspect a single run, "
        f"compare a few, or look at the raw journal."
    )


def render_recent_runs(runs: pd.DataFrame) -> None:
    """A compact list of the 10 most recent runs at the top of the page,
    so the operator can see what's in the journal at a glance.

    Uses explicit pd.notna checks because run rows can have NaN for
    n_trades / sum_realized_pnl when a run was started but never finished
    (storage.save_run inserts the row before storage.finish_run fills
    those columns)."""
    if runs is None or runs.empty:
        return

    def _safe_int(v) -> int:
        return int(v) if pd.notna(v) else 0

    def _safe_float(v) -> float:
        return float(v) if pd.notna(v) else 0.0

    def _safe_bool(v) -> bool:
        # SQLite stores reconciles as 0/1, NaN if NULL → treat NaN as False
        return bool(v) if pd.notna(v) else False

    with st.expander(f"📚  Recent runs in journal ({len(runs)})",
                       expanded=False):
        rows = []
        for _, r in runs.head(20).iterrows():
            rows.append({
                "started": str(r.get("started_at_utc") or "")[:16],
                "strategy": r.get("strategy") or "—",
                "symbol": r.get("symbol") or "—",
                "tf": r.get("tf") or "—",
                "trades": _safe_int(r.get("n_trades")),
                "$ pnl": round(_safe_float(r.get("sum_realized_pnl")), 2),
                "start_bal": round(_safe_float(r.get("starting_balance"))),
                "reconciles": "✓" if _safe_bool(r.get("reconciles")) else "⛔",
                "run_id": r.get("run_id"),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch",
                       height=min(420, 36 * (len(rows) + 1)))


def render_projected_from_library() -> None:
    """When the journal is empty, show what the recommended portfolio
    is *expected* to deliver based on grid_results.md stats.

    This is NOT a forecast — it's "if every recommended strategy
    delivered its OOS stats this month, this is the picture you'd
    see". Operators use this to sanity-check whether the portfolio
    even targets the FTMO profit goal."""
    from core import strategy_library

    lib = [e for e in strategy_library.list_library() if e.recommended]
    if not lib:
        return
    rows = []
    for e in lib:
        if not e.edge:
            continue
        rows.append({
            "strategy": e.strategy,
            "ticker": e.ticker,
            "tf": e.tf,
            "n_test": e.edge.n_test,
            "PF_test": round(e.edge.test_pf, 2),
            "R_test (OOS)": round(e.edge.test_r, 3),
            "win_rate ≈": (
                f"{(0.5 + e.edge.test_r / 4) * 100:.0f}%"
                if -2 < e.edge.test_r < 2 else "—"),
            "why": e.why,
        })
    if not rows:
        return
    st.markdown("### 🔮  Expected stats from the recommended portfolio")
    st.caption(
        "OOS R-multiples from `make sweep-grid` on real broker data. "
        "These are **out-of-sample** — the strategy was fit on the first "
        "60% of bars, evaluated on the last 40%. Win rate is a rough "
        "proxy from R; verify per strategy on the Strategy Library "
        "scatter plot.")
    st.dataframe(pd.DataFrame(rows), width="stretch",
                   height=min(360, 36 * (len(rows) + 1)))


def main():
    st.set_page_config(page_title="Performance", page_icon="📈",
                        layout="wide")
    st.title("📈  Performance")
    st.caption(
        "Every closed trade across **backtest / paper / live**, with "
        "P&L curve, drawdown, R distribution, attribution, and a monthly "
        "heat map. **Default scope = the most recent single run** "
        "(the trade journal accumulates every backtest, so summing them "
        "all together would add apples to oranges)."
    )
    # Bug D fix (2026-05-08): resolve per-account DB instead of legacy
    # main repo DB so Performance reads from the same place the runner
    # writes trades. Falls back to main DB only when no account is set.
    try:
        from core import account_manager
        accounts = account_manager.list_accounts()
        if accounts:
            db_path = str(account_manager.get_db_path(accounts[0].login))
        else:
            db_path = str(REPO / "data" / "v2.db")
    except Exception:
        db_path = str(REPO / "data" / "v2.db")
    runs = _load_runs(db_path)
    df_all = _load_trades(db_path)

    if df_all.empty and (runs is None or runs.empty):
        render_basis_explainer(df_all, runs)
        st.markdown("---")
        render_projected_from_library()
        return

    render_recent_runs(runs)

    df = render_filters(df_all, runs)
    render_basis_explainer(df, runs)

    # ── Phase-32: separate paper / live / backtest into tabs ──────────
    # User asked: "should we separate paper and live and backtest
    # rather mixing them up". Mixing was correct for the OLD basis
    # caption (one combined table) but masked which trades came from
    # which mode. Now: 4 tabs — All / Live / Paper / Backtest — each
    # with its own metrics + charts + journal. Default tab is the
    # most relevant (live if any live trades; else paper; else all).
    n_live = int((df["mode"] == "live").sum())
    n_paper = int((df["mode"] == "paper").sum())
    n_bt = int((df["mode"] == "backtest").sum())
    tab_labels = [
        f"📊 All ({len(df)})",
        f"🟢 Live ({n_live})",
        f"🟡 Paper ({n_paper})",
        f"🧪 Backtest ({n_bt})",
    ]
    tab_all, tab_live, tab_paper, tab_bt = st.tabs(tab_labels)

    def _render_tab(df_tab: pd.DataFrame, label: str) -> None:
        if df_tab.empty:
            st.info(
                f"No {label} trades match these filters. "
                f"Switch to another tab or widen the date range."
            )
            return
        render_metrics(df_tab)
        render_equity(df_tab, key_suffix=label)
        sub_cols = st.columns(2)
        with sub_cols[0]:
            render_r_distribution(df_tab, key_suffix=label)
        with sub_cols[1]:
            render_attribution_pie(df_tab, key_suffix=label)
        render_monthly_heatmap(df_tab, key_suffix=label)
        render_journal_table(df_tab, db_path, key_suffix=label)

    with tab_all:
        st.caption(
            "Combined view across all modes. Useful for total system "
            "performance — but check the per-mode tabs to confirm "
            "which mode is actually contributing."
        )
        _render_tab(df, "all")
    with tab_live:
        st.caption(
            "🟢 **Live trades only** — real money committed via the broker. "
            "This is the truth signal for whether your system is working "
            "in production. Compare to the Catalog projection to verify "
            "live matches backtest."
        )
        _render_tab(df[df["mode"] == "live"], "live")
    with tab_paper:
        st.caption(
            "🟡 **Paper trades only** — same strategy + same data as live "
            "but executed against the paper executor (no broker)."
        )
        _render_tab(df[df["mode"] == "paper"], "paper")
    with tab_bt:
        st.caption(
            "🧪 **Backtest trades only** — historical replay. The Catalog "
            "and Strategy Library numbers are derived from these."
        )
        _render_tab(df[df["mode"] == "backtest"], "backtest")


main()
