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
    """Render sidebar filters. The DEFAULT now scopes to the most-recent
    single run, not every backtest ever — otherwise the page conflates
    apples and oranges (different starting balances, different lot
    sizes, different strategies)."""
    st.sidebar.markdown("### 🔎  Filters")
    if df.empty:
        return df

    # ── Run picker (the big change) ──
    st.sidebar.markdown("**Scope**")
    scope_options = ["Single run (most recent)", "Compare runs",
                      "All trades (raw journal)"]
    scope = st.sidebar.radio("scope", scope_options, index=0,
                              key="perf_scope",
                              label_visibility="collapsed")

    f = df.copy()
    if scope == "Single run (most recent)":
        if not runs.empty:
            run_labels = {r["run_id"]: _run_label(r)
                           for _, r in runs.iterrows()}
            chosen = st.sidebar.selectbox(
                "run", options=list(run_labels.keys()),
                format_func=lambda rid: run_labels[rid],
                index=0, key="perf_single_run",
            )
            f = f[f["run_id"] == chosen]
    elif scope == "Compare runs":
        if not runs.empty:
            run_labels = {r["run_id"]: _run_label(r)
                           for _, r in runs.iterrows()}
            chosen = st.sidebar.multiselect(
                "runs", options=list(run_labels.keys()),
                format_func=lambda rid: run_labels[rid],
                default=list(run_labels.keys())[:3],
                key="perf_multi_run",
            )
            if chosen:
                f = f[f["run_id"].isin(chosen)]
    # else: scope == "All trades" → no run filter

    # Secondary attribute filters (within whatever runs we kept)
    st.sidebar.markdown("**Attributes**")
    modes = sorted(f["mode"].dropna().unique().tolist())
    sel_modes = st.sidebar.multiselect("mode", modes, default=modes,
                                          key="perf_modes")
    strats = sorted(f["strategy"].dropna().unique().tolist())
    sel_strats = st.sidebar.multiselect("strategy", strats, default=strats,
                                           key="perf_strats")
    syms = sorted(f["symbol"].dropna().unique().tolist())
    sel_syms = st.sidebar.multiselect("symbol", syms, default=syms,
                                         key="perf_syms")
    reasons = sorted(f["close_reason"].dropna().unique().tolist())
    sel_reasons = st.sidebar.multiselect("close_reason", reasons,
                                            default=reasons, key="perf_reasons")
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


def render_metrics(df: pd.DataFrame):
    n = len(df)
    if n == 0:
        st.info("No trades match these filters.")
        return
    pnl = float(df["pnl"].sum())
    wins = (df["pnl"] > 0).sum()
    win_rate = wins / n * 100
    gw = df.loc[df["pnl"] > 0, "pnl"].sum()
    gl = -df.loc[df["pnl"] <= 0, "pnl"].sum()
    pf = gw / gl if gl > 0 else float("inf") if gw > 0 else 0
    avg_R = df["R"].mean()
    cols = st.columns(5)
    cols[0].metric("trades", n)
    cols[1].metric("win rate", f"{win_rate:.1f}%")
    pf_text = "inf" if pf == float("inf") else f"{pf:.2f}"
    cols[2].metric("profit factor", pf_text)
    cols[3].metric("avg R", f"{avg_R:+.3f}")
    cols[4].metric("net P&L", f"${pnl:+,.2f}")


def render_equity(df: pd.DataFrame):
    if df.empty:
        return
    df_s = df.sort_values("closed_at_utc").copy()
    df_s["cum_pnl"] = df_s["pnl"].cumsum()
    fig = go.Figure(go.Scatter(
        x=df_s["closed_at_utc"], y=df_s["cum_pnl"],
        mode="lines", line=dict(color="#0a4", width=1.5),
    ))
    fig.update_layout(title="Cumulative P&L (filtered)",
                       xaxis_title="time", yaxis_title="$",
                       height=320, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)
    # Drawdown
    peak = df_s["cum_pnl"].cummax()
    dd = df_s["cum_pnl"] - peak
    fig2 = go.Figure(go.Scatter(
        x=df_s["closed_at_utc"], y=dd, mode="lines",
        line=dict(color="#c33", width=0.8), fill="tozeroy",
        fillcolor="rgba(204,51,51,0.25)",
    ))
    fig2.update_layout(title="Drawdown ($)",
                        xaxis_title="time", yaxis_title="$",
                        height=180, margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig2, use_container_width=True)


def render_r_distribution(df: pd.DataFrame):
    if df.empty:
        return
    fig = go.Figure(go.Histogram(
        x=df["R"], nbinsx=40, marker_color="#3a8ac4",
    ))
    fig.update_layout(title="R-multiple distribution",
                       xaxis_title="R", yaxis_title="trades",
                       height=300, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_monthly_heatmap(df: pd.DataFrame):
    if df.empty or df["closed_at_utc"].isna().all():
        return
    df = df.copy()
    df["year"] = df["closed_at_utc"].dt.year
    df["month"] = df["closed_at_utc"].dt.month
    monthly = df.groupby(["year", "month"])["pnl"].sum().reset_index()
    pivot = monthly.pivot(index="year", columns="month", values="pnl")
    if pivot.empty:
        return
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=pivot.columns, y=pivot.index,
        colorscale="RdYlGn", zmid=0,
        text=[[f"${v:+,.0f}" if pd.notna(v) else "" for v in row]
               for row in pivot.values],
        texttemplate="%{text}", textfont=dict(size=10),
        colorbar=dict(title="$"),
    ))
    fig.update_layout(title="Monthly P&L heatmap (year × month)",
                       xaxis_title="month", yaxis_title="year",
                       height=320, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_attribution_pie(df: pd.DataFrame):
    if df.empty:
        return
    by_strat = df.groupby("strategy")["pnl"].sum().sort_values(ascending=False)
    fig = go.Figure(go.Pie(
        labels=by_strat.index.tolist(),
        values=by_strat.values.tolist(),
        hole=0.45,
    ))
    fig.update_layout(title="P&L attribution by strategy",
                       height=320, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)


def render_journal_table(df: pd.DataFrame, db_path: str):
    if df.empty:
        return
    st.markdown("---")
    st.markdown("**Trade journal** — edit `note` cells; press Enter to save.")
    edited = st.data_editor(
        df[["closed_at_utc", "mode", "strategy", "symbol", "tf",
            "direction", "entry", "exit", "lots", "pnl", "R",
            "close_reason", "note", "run_id", "trade_idx"]],
        use_container_width=True, height=480,
        disabled=["closed_at_utc", "mode", "strategy", "symbol", "tf",
                  "direction", "entry", "exit", "lots", "pnl", "R",
                  "close_reason", "run_id", "trade_idx"],
        key="perf_journal_editor",
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
    so the operator can see what's in the journal at a glance."""
    if runs is None or runs.empty:
        return
    with st.expander(f"📚  Recent runs in journal ({len(runs)})",
                       expanded=False):
        rows = []
        for _, r in runs.head(20).iterrows():
            rows.append({
                "started": str(r["started_at_utc"])[:16],
                "strategy": r["strategy"],
                "symbol": r["symbol"],
                "tf": r["tf"],
                "trades": int(r["n_trades"] or 0),
                "$ pnl": round(float(r["sum_realized_pnl"] or 0.0), 2),
                "start_bal": round(float(r["starting_balance"] or 0.0)),
                "reconciles": "✓" if r["reconciles"] else "⛔",
                "run_id": r["run_id"],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True,
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
    st.dataframe(pd.DataFrame(rows), use_container_width=True,
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

    render_metrics(df)
    render_equity(df)
    cols = st.columns(2)
    with cols[0]:
        render_r_distribution(df)
    with cols[1]:
        render_attribution_pie(df)
    render_monthly_heatmap(df)
    render_journal_table(df, db_path)


main()
