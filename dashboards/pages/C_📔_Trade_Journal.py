"""
C_📔_Trade_Journal.py — unified trade journal across PAPER and LIVE.

TradeZilla-style: every closed trade in one place, segregated by mode
so paper exploration never gets mixed with live capital. Designed to
answer "how is each account growing?" at a glance.

Sections:
  1. Mode toggle  — paper / live / both
  2. Headline KPIs — total P&L, win rate, avg R, max DD, streak
  3. Equity curve — running cumulative + drawdown shaded
  4. Daily P&L bars — green/red columns per UTC day
  5. R-multiple distribution — histogram of trade outcomes
  6. Per-strategy contribution — sortable table
  7. Trade journal table — every trade with filters
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

from core import account_manager                               # noqa: E402
from core import perf_query                                     # noqa: E402
from dashboards.components import theme                         # noqa: E402


# Bug D fix (2026-05-08): per-account DB, not the legacy main repo DB.
def _resolve_active_db_path():
    try:
        accounts = account_manager.list_accounts()
        if accounts:
            return account_manager.get_db_path(accounts[0].login)
    except Exception:
        pass
    return REPO / "data" / "v2.db"


DB_PATH = _resolve_active_db_path()

# Defensive check at page load — if Streamlit is running on a cached
# version of perf_query.py without journal_trades, fail with an actionable
# message instead of a bare AttributeError 200 lines deeper.
_REQUIRED_PERF_QUERY_FNS = (
    "journal_trades", "daily_pnl", "streak_analysis", "headline_kpis",
)
_missing = [f for f in _REQUIRED_PERF_QUERY_FNS
              if not hasattr(perf_query, f)]
if _missing:
    st.set_page_config(page_title="Trade Journal", page_icon="📔",
                          layout="wide")
    st.title("📔  Trade Journal")
    st.error(
        f"⛔ Stale module detected — `core.perf_query` is missing "
        f"{_missing}.\n\n"
        f"This means your Streamlit server is running an old cached "
        f"version of the module from before the journal page was added.\n\n"
        f"**Fix:** stop the dashboard (Ctrl+C in the terminal) and "
        f"restart with `streamlit run dashboards/pages/C_📔_Trade_Journal.py` "
        f"or `make dashboard`. Streamlit's hot-reloader does NOT pick up "
        f"new top-level functions in already-imported modules; only a "
        f"restart fully reloads."
    )
    import sys as _sys
    _sys.exit(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _money(v: float, sign: bool = False) -> str:
    return ("${:+,.0f}" if sign else "${:,.0f}").format(v or 0.0)


def _equity_curve_chart(df_curve: pd.DataFrame, *,
                          starting_balance: float,
                          title: str,
                          color: str = "#16a34a") -> go.Figure:
    """Equity-curve chart with peak line + drawdown shading."""
    fig = go.Figure()
    if df_curve.empty:
        fig.add_annotation(
            text="No trades yet — run a strategy in paper or live first.",
            x=0.5, y=0.5, xref="paper", yref="paper",
            showarrow=False, font=dict(color="#9ca3af", size=14),
        )
        fig.update_layout(height=320, plot_bgcolor="#0b1117",
                            paper_bgcolor="#0b1117",
                            font=dict(color="#cbd5e1"))
        return fig

    # Equity curve
    fig.add_trace(go.Scatter(
        x=df_curve["time_utc"], y=df_curve["equity"],
        mode="lines", name="Equity",
        line=dict(color=color, width=2.2),
        hovertemplate="%{x|%Y-%m-%d %H:%M}<br>$%{y:,.2f}<extra></extra>",
    ))
    # Running peak
    df_curve = df_curve.copy()
    df_curve["peak"] = df_curve["equity"].cummax()
    fig.add_trace(go.Scatter(
        x=df_curve["time_utc"], y=df_curve["peak"],
        mode="lines", name="Peak",
        line=dict(color="#9ca3af", width=1, dash="dot"),
        hoverinfo="skip",
    ))
    # Starting balance reference
    fig.add_hline(y=starting_balance,
                    line=dict(color="#6b7280", dash="dash"),
                    annotation_text=f"start ${starting_balance:,.0f}",
                    annotation_position="bottom right",
                    annotation_font=dict(color="#6b7280", size=10))
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=40, b=10),
        title=title,
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937", title=None),
        yaxis=dict(gridcolor="#1f2937", title="$"),
        legend=dict(orientation="h", x=0, y=1.10),
        hovermode="x unified",
    )
    return fig


def _daily_pnl_chart(df_daily: pd.DataFrame, *, title: str) -> go.Figure:
    fig = go.Figure()
    if df_daily.empty:
        fig.add_annotation(text="No trades yet.", x=0.5, y=0.5,
                              xref="paper", yref="paper",
                              showarrow=False, font=dict(color="#9ca3af"))
        fig.update_layout(height=240, plot_bgcolor="#0b1117",
                            paper_bgcolor="#0b1117")
        return fig
    colors = ["#16a34a" if v >= 0 else "#dc2626"
              for v in df_daily["pnl"].tolist()]
    fig.add_trace(go.Bar(
        x=df_daily["date"], y=df_daily["pnl"],
        marker_color=colors,
        text=[f"${v:+,.0f}" for v in df_daily["pnl"]],
        textposition="outside",
        hovertemplate="%{x}<br>$%{y:,.2f} on %{customdata} trades"
                       "<extra></extra>",
        customdata=df_daily["n_trades"],
        name="daily P&L",
    ))
    fig.add_hline(y=0, line=dict(color="#6b7280", dash="solid", width=1))
    fig.update_layout(
        height=260, margin=dict(l=10, r=10, t=36, b=10),
        title=title,
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937"),
        yaxis=dict(gridcolor="#1f2937", title="$"),
        showlegend=False,
    )
    return fig


def _r_distribution_chart(df_trades: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if df_trades.empty or df_trades["r_multiple"].isna().all():
        fig.add_annotation(text="No R-multiple data.",
                              x=0.5, y=0.5, xref="paper", yref="paper",
                              showarrow=False, font=dict(color="#9ca3af"))
        fig.update_layout(height=260, plot_bgcolor="#0b1117",
                            paper_bgcolor="#0b1117")
        return fig
    r_vals = df_trades["r_multiple"].dropna().tolist()
    fig.add_trace(go.Histogram(
        x=r_vals, nbinsx=30,
        marker=dict(color="#3b82f6", line=dict(color="#1e3a8a", width=1)),
        hovertemplate="R %{x:.2f}<br>%{y} trades<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color="#6b7280", dash="dash"))
    if r_vals:
        avg_r = sum(r_vals) / len(r_vals)
        fig.add_vline(x=avg_r,
                        line=dict(color="#16a34a" if avg_r > 0 else "#dc2626",
                                    dash="solid", width=2),
                        annotation_text=f"avg {avg_r:+.2f}R",
                        annotation_position="top right",
                        annotation_font=dict(
                            color="#16a34a" if avg_r > 0 else "#dc2626",
                            size=11,
                        ))
    fig.update_layout(
        height=300, margin=dict(l=10, r=10, t=36, b=10),
        title="R-multiple distribution",
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937", title="R-multiple"),
        yaxis=dict(gridcolor="#1f2937", title="# trades"),
        showlegend=False,
    )
    return fig


def _per_strategy_table(df_trades: pd.DataFrame) -> pd.DataFrame:
    if df_trades.empty:
        return pd.DataFrame()
    grp = df_trades.groupby(["strategy", "symbol", "tf", "mode"], dropna=False)
    rows = []
    for (strat, sym, tf, mode), g in grp:
        wins = g[g["realized_pnl"] > 0]
        losses = g[g["realized_pnl"] < 0]
        n = len(g)
        rows.append({
            "Strategy": strat or "?",
            "Ticker": sym or "?",
            "TF": tf or "?",
            "Mode": mode,
            "Trades": n,
            "Win%": round(len(wins) / n * 100.0, 1) if n else 0.0,
            "Total P&L": round(float(g["realized_pnl"].sum()), 2),
            "Avg R": (round(float(g["r_multiple"].mean()), 2)
                       if g["r_multiple"].notna().any() else None),
            "Avg win": round(float(wins["realized_pnl"].mean()), 2)
                        if len(wins) else 0.0,
            "Avg loss": round(float(losses["realized_pnl"].mean()), 2)
                          if len(losses) else 0.0,
            "Last trade": (str(g["closed_at_utc"].max())[:16]
                            if g["closed_at_utc"].notna().any() else "—"),
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("Total P&L", ascending=False).reset_index(drop=True)
    return df


def _render_kpi_row(k: dict, label: str) -> None:
    st.markdown(f"#### {label}")
    cols = st.columns(6)
    cols[0].metric(
        "Total P&L",
        _money(k["total_pnl"], sign=True),
        help=f"Across {k['n_trades']} closed trades.",
    )
    cols[1].metric(
        "Equity",
        _money(k["current_equity"]),
        delta=f"{(k['current_equity']/k['starting_balance']-1)*100:+.2f}%",
        help=f"From ${k['starting_balance']:,.0f} baseline.",
    )
    cols[2].metric(
        "Win rate",
        f"{k['win_rate_pct']:.1f}%",
        help="Closed trades with realized_pnl > 0.",
    )
    cols[3].metric(
        "Avg R",
        f"{k['avg_r']:+.2f}",
        help="Mean R-multiple per trade. > 0 = positive expectancy.",
    )
    cols[4].metric(
        "Max DD",
        _money(k["max_dd_dollars"]),
        delta=f"-{k['max_dd_pct']:.2f}%",
        delta_color="inverse",
        help="Peak-to-trough on the trade-by-trade equity curve.",
    )
    streak_color = ("normal" if k["current_kind"] == "winning"
                     else "inverse" if k["current_kind"] == "losing"
                     else "off")
    cols[5].metric(
        "Streak",
        f"{k['current_streak']} {k['current_kind']}",
        help="Current consecutive winners / losers (most recent trades).",
        delta_color=streak_color,
    )


def _live_broker_snapshot() -> dict | None:
    """Fetch real account_info + open positions from the MT5 bridge.
    Returns None if no account configured or bridge offline.

    The Trade Journal previously showed a static $100k baseline equity
    even on the LIVE section — misleading. Now we ground LIVE on the
    broker's actual balance/equity/floating P&L.
    """
    try:
        from core import account_manager
        from core.mt5_account import MT5AccountClient
        accounts = account_manager.list_accounts()
        if not accounts:
            return None
        active = accounts[0]
        client = MT5AccountClient()
        info = client.account_info(force_refresh=True)
        try:
            positions = client.positions_get()
        except Exception:
            positions = []
        return {
            "login": active.login,
            "alias": active.alias,
            "balance": float(info.balance),
            "equity": float(info.equity),
            "margin": float(info.margin),
            "margin_free": float(info.margin_free),
            "currency": info.currency,
            "positions": positions,
        }
    except Exception as e:
        return {"error": str(e)}


def _render_open_positions_panel(positions: list, *,
                                    label: str = "🟢 LIVE") -> None:
    """Compact open-positions table inline in the journal.

    Uses real bridge fields (BridgePosition shape: ticket, symbol, type,
    volume, price_open, sl, tp, price_current, profit, comment).

    For full Close / Close-All controls, link the user to Operations →
    💼 Positions tab — that panel has the typed-confirm flow + retry
    logic + parity-aware refresh.
    """
    if not positions:
        st.info(
            f"_No open positions on the broker right now ({label} account)._"
        )
        return
    rows = []
    for p in positions:
        side = "LONG" if int(getattr(p, "type", 0)) == 0 else "SHORT"
        pnl = float(getattr(p, "profit", 0.0) or 0.0)
        rows.append({
            "ticket": int(getattr(p, "ticket", 0)),
            "symbol": getattr(p, "symbol", ""),
            "side": side,
            "lots": float(getattr(p, "volume", 0.0)),
            "entry": round(float(getattr(p, "price_open", 0.0)), 5),
            "now": round(float(getattr(p, "price_current", 0.0)), 5),
            "SL": round(float(getattr(p, "sl", 0.0)), 5)
                  if getattr(p, "sl", 0.0) else None,
            "TP": round(float(getattr(p, "tp", 0.0)), 5)
                  if getattr(p, "tp", 0.0) else None,
            "$ pnl": round(pnl, 2),
            "comment": getattr(p, "comment", "")[:30],
        })
    df = pd.DataFrame(rows)

    def _color_pnl(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return ""
        if f > 0:
            return "color: #16a34a; font-weight: 600;"
        if f < 0:
            return "color: #dc2626; font-weight: 600;"
        return "color: #9ca3af;"

    styled = (df.style
                .map(_color_pnl, subset=["$ pnl"])
                .format({"entry": "{:.5f}", "now": "{:.5f}",
                            "SL": "{:.5f}", "TP": "{:.5f}",
                            "$ pnl": "${:+,.2f}"}, na_rep="—"))
    total_pnl = sum(float(getattr(p, "profit", 0.0) or 0.0)
                      for p in positions)
    pnl_color = "#16a34a" if total_pnl >= 0 else "#dc2626"
    st.markdown(
        f"**{len(positions)} open position(s)** · "
        f"floating P&L "
        f"<span style='color:{pnl_color};font-weight:700;'>"
        f"${total_pnl:+,.2f}</span>",
        unsafe_allow_html=True,
    )
    st.dataframe(styled, width="stretch",
                   height=min(360, 36 * (len(df) + 1)))
    st.caption(
        "💡 To close one or all positions, head to **🚀 Operations → "
        "💼 Positions tab** — full Close / Close-All flow with retry "
        "and detailed error info lives there. SL/TP shown above are "
        "**stored on the broker** (server-side) — they fire even if "
        "Python is offline."
    )


def _render_mode_section(label: str, mode: str,
                           starting_balance: float,
                           color: str,
                           broker_snapshot: dict | None = None) -> None:
    """One section showing equity curve + daily P&L for a single mode.

    When `broker_snapshot` is provided AND mode == 'live', use the real
    broker balance/equity/floating P&L instead of computed-from-closed
    -trades. This is critical for live: the equity curve must match
    what the user sees in their MT5 terminal.
    """
    # Override starting_balance with the broker's REAL deposit baseline
    # if we have it. The kpi computation still uses the closed-trade
    # cumsum to estimate "current equity", but the broker's equity is
    # shown as the source-of-truth.
    k = perf_query.headline_kpis(DB_PATH, mode=mode,
                                    starting_balance=starting_balance)

    # Inject broker-real equity if available (LIVE only)
    real_equity_from_broker = None
    if (mode == "live" and broker_snapshot is not None
        and "error" not in broker_snapshot):
        real_equity_from_broker = broker_snapshot.get("equity")
        # Use broker balance as the "starting" reference for live too
        # — that's what FTMO traders care about.
        bal = broker_snapshot.get("balance", 0.0)
        if bal > 0:
            k = dict(k)  # don't mutate the perf_query dict
            k["starting_balance"] = bal
            k["current_equity"] = real_equity_from_broker

    _render_kpi_row(k, label)

    # ---- Live broker info strip ----
    if (mode == "live" and broker_snapshot is not None
        and "error" not in broker_snapshot):
        bs = broker_snapshot
        cols = st.columns(4)
        cols[0].metric("Broker balance",
                          f"${bs['balance']:,.2f}",
                          help="Closed-trade balance from MT5 (excludes "
                                "floating P&L on open positions).")
        cols[1].metric("Broker equity",
                          f"${bs['equity']:,.2f}",
                          delta=f"{(bs['equity'] - bs['balance']):+,.2f} "
                                  f"floating",
                          help="Real-time equity from MT5 = balance + "
                                "floating P&L on currently open positions.")
        cols[2].metric("Margin used",
                          f"${bs['margin']:,.2f}",
                          help="Capital reserved for currently open positions.")
        cols[3].metric("Free margin",
                          f"${bs['margin_free']:,.2f}",
                          help="Available capital for new positions.")
        st.caption(
            f"💡 Live data from broker `{bs['alias']}` (#{bs['login']}) — "
            f"this is **what your MT5 terminal shows right now**. "
            f"Refreshes on every page load."
        )
    elif mode == "live" and broker_snapshot and "error" in broker_snapshot:
        st.warning(
            f"⚠ Could not fetch live broker data: "
            f"`{broker_snapshot['error']}`. Showing journal-computed "
            f"equity instead. Check the bridge on the Operations page."
        )

    # ---- Open positions table (LIVE only) ----
    if (mode == "live" and broker_snapshot is not None
        and "error" not in broker_snapshot):
        st.markdown("##### 💼 Currently open positions")
        _render_open_positions_panel(broker_snapshot["positions"],
                                        label=label)

    # ---- Closed-trade analytics ----
    if k["n_trades"] == 0:
        st.info(
            f"_No closed {mode} trades yet. Run a strategy from the "
            f"**{'🟡 Paper' if mode == 'paper' else '🟢 Live'}** "
            f"page to populate the equity curve / daily P&L charts._"
        )
        return

    curve = perf_query.equity_curve(DB_PATH, mode=mode,
                                       starting_balance=k["starting_balance"])
    daily = perf_query.daily_pnl(DB_PATH, mode=mode)
    cols = st.columns([3, 2])
    with cols[0]:
        st.plotly_chart(
            _equity_curve_chart(curve, starting_balance=k["starting_balance"],
                                  title=f"Equity curve — {label}",
                                  color=color),
            width="stretch",
        )
    with cols[1]:
        st.plotly_chart(
            _daily_pnl_chart(daily, title=f"Daily P&L — {label}"),
            width="stretch",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Trade Journal", page_icon="📔",
                          layout="wide")
    theme.inject_css()
    st.title("📔  Trade Journal")
    st.caption(
        "Every closed trade across **paper** and **live**, segregated "
        "by mode so paper exploration never mixes with live capital. "
        "Equity curves, daily P&L, R-distribution, and a filterable "
        "trade table — TradeZilla-style."
    )

    # ── Sidebar controls ──────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### 📔  Journal config")
        view_mode = st.radio(
            "View",
            ["both", "paper", "live"],
            index=0, horizontal=True,
            help="Both = paper + live side-by-side. Paper / live = "
                  "single-mode focus.",
        )
        starting_balance = float(st.number_input(
            "Baseline equity $",
            value=100_000.0, min_value=1_000.0, step=1_000.0,
            help="Used to anchor the equity curve. The curve starts here "
                  "and steps up/down with each closed trade.",
        ))
        st.markdown("---")
        st.markdown("**Trade table filters**")
        # Pull strategy/ticker/tf options from journal across both modes
        all_trades = perf_query.journal_trades(DB_PATH)
        if not all_trades.empty:
            strat_opts = sorted(all_trades["strategy"].dropna().unique())
            ticker_opts = sorted(all_trades["symbol"].dropna().unique())
            tf_opts = sorted(all_trades["tf"].dropna().unique())
        else:
            strat_opts, ticker_opts, tf_opts = [], [], []
        f_strats = st.multiselect(
            "Strategy", strat_opts, default=[],
            help="Empty = all strategies.")
        f_tickers = st.multiselect(
            "Ticker", ticker_opts, default=[],
            help="Empty = all tickers.")
        f_tfs = st.multiselect(
            "Timeframe", tf_opts, default=[],
            help="Empty = all timeframes.")
        f_only_winners = st.checkbox("Only winners", value=False)
        f_only_losers = st.checkbox("Only losers", value=False)
        if f_only_winners and f_only_losers:
            st.warning("Both filters on = empty result. Untick one.")

    # ── Pull broker snapshot ONCE (used by LIVE section if visible) ──
    broker_snapshot = None
    if view_mode in ("both", "live"):
        with st.spinner("Reading live broker data..."):
            broker_snapshot = _live_broker_snapshot()

    # ── Combined headline (always visible — gives total picture) ──────
    if view_mode == "both":
        # Render both modes — LIVE first (real account = priority view)
        st.markdown("---")
        _render_mode_section("🟢 LIVE", "live",
                                starting_balance=starting_balance,
                                color="#16a34a",
                                broker_snapshot=broker_snapshot)
        st.markdown("---")
        _render_mode_section("🟡 PAPER", "paper",
                                starting_balance=starting_balance,
                                color="#fbbf24")
    else:
        st.markdown("---")
        color = "#16a34a" if view_mode == "live" else "#fbbf24"
        label = "🟢 LIVE" if view_mode == "live" else "🟡 PAPER"
        _render_mode_section(label, view_mode,
                                starting_balance=starting_balance,
                                color=color,
                                broker_snapshot=broker_snapshot
                                if view_mode == "live" else None)

    # ── R-multiple distribution + per-strategy contribution ────────────
    st.markdown("---")
    st.markdown("### 📊  Distribution & per-strategy")
    if view_mode == "both":
        modes_to_query = ["paper", "live"]
    else:
        modes_to_query = [view_mode]
    df_filtered = perf_query.journal_trades(
        DB_PATH,
        modes=modes_to_query,
        strategy=f_strats[0] if len(f_strats) == 1 else None,
        ticker=f_tickers[0] if len(f_tickers) == 1 else None,
        tf=f_tfs[0] if len(f_tfs) == 1 else None,
    )
    # multi-select filtering happens in-pandas (DB query only handles single)
    if not df_filtered.empty:
        if f_strats:
            df_filtered = df_filtered[df_filtered["strategy"].isin(f_strats)]
        if f_tickers:
            df_filtered = df_filtered[df_filtered["symbol"].isin(f_tickers)]
        if f_tfs:
            df_filtered = df_filtered[df_filtered["tf"].isin(f_tfs)]
        if f_only_winners and not f_only_losers:
            df_filtered = df_filtered[df_filtered["realized_pnl"] > 0]
        if f_only_losers and not f_only_winners:
            df_filtered = df_filtered[df_filtered["realized_pnl"] < 0]

    cols = st.columns([3, 2])
    with cols[0]:
        per_strat = _per_strategy_table(df_filtered)
        if per_strat.empty:
            st.info("_No trades match the filters._")
        else:
            # Color the P&L column
            def _pnl_color(v):
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return ""
                if f > 0:
                    return "color: #16a34a; font-weight: 600;"
                if f < 0:
                    return "color: #dc2626; font-weight: 600;"
                return ""
            styled = per_strat.style.map(
                _pnl_color, subset=["Total P&L"]
            ).format({"Total P&L": "${:+,.2f}",
                          "Avg win": "${:+,.2f}",
                          "Avg loss": "${:+,.2f}"})
            st.markdown("**Per-strategy P&L (filtered)**")
            st.dataframe(styled, width="stretch",
                            height=min(420, 38 * (len(per_strat) + 1)))
    with cols[1]:
        st.plotly_chart(_r_distribution_chart(df_filtered),
                          width="stretch")

    # ── Trade table ───────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(f"### 📜  Trade journal "
                  f"({len(df_filtered):,} trade{'s' if len(df_filtered) != 1 else ''})")
    if df_filtered.empty:
        st.info(
            "_No closed trades match the current filters. Adjust the "
            "filters in the sidebar, or run some trades on the "
            "Paper / Live page._"
        )
    else:
        # Build a display-friendly view of the trades table
        display = df_filtered.copy()
        display["Time"] = display["closed_at_utc"].dt.strftime("%Y-%m-%d %H:%M")
        display["Mode"] = display["mode"].str.upper()
        display["Trade"] = (
            display["strategy"].fillna("?") + " · "
            + display["symbol"].fillna("?") + " · "
            + display["tf"].fillna("?")
        )
        display["Side"] = display["direction"]
        display["P&L"] = display["realized_pnl"].round(2)
        display["R"] = display["r_multiple"].round(2)
        display["Reason"] = display["close_reason"].fillna("?")
        display["Duration"] = display["duration_min"].apply(
            lambda m: (f"{m:.0f}m" if pd.notna(m) and m < 60
                       else f"{m/60:.1f}h" if pd.notna(m) and m < 1440
                       else f"{m/1440:.1f}d" if pd.notna(m)
                       else "?")
        )
        display["Entry"] = display["entry_price"].round(4)
        display["Exit"] = display["exit_price"].round(4)
        display["Lots"] = display["lots"].round(3)
        cols_show = ["Time", "Mode", "Trade", "Side", "Entry", "Exit",
                       "Lots", "Duration", "P&L", "R", "Reason"]
        display = display[cols_show]

        # Color P&L and R columns
        def _pnl_style(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return ""
            if f > 0:
                return "color: #16a34a; font-weight: 600;"
            if f < 0:
                return "color: #dc2626; font-weight: 600;"
            return ""

        def _mode_style(v):
            if v == "LIVE":
                return "color: #16a34a; font-weight: 700;"
            if v == "PAPER":
                return "color: #fbbf24; font-weight: 700;"
            return ""

        styled = (display.style
                    .map(_pnl_style, subset=["P&L", "R"])
                    .map(_mode_style, subset=["Mode"])
                    .format({"P&L": "${:+,.2f}", "R": "{:+.2f}",
                                "Entry": "{:,.4f}", "Exit": "{:,.4f}"}))
        st.dataframe(styled, width="stretch",
                       height=min(700, 38 * (len(display) + 1)))

        # Download as CSV
        csv = df_filtered.to_csv(index=False).encode("utf-8")
        st.download_button(
            "📥 Download as CSV",
            data=csv,
            file_name="trade_journal.csv",
            mime="text/csv",
            help="Export the filtered trades for external analysis.",
        )

    # ── Footer caption ────────────────────────────────────────────────
    st.markdown("---")
    st.caption(
        "💡 Trade journal reads from `data/v2.db` — every closed paper "
        "or live trade lands here automatically. Backtest replay trades "
        "are excluded so the journal reflects real (or paper-real) "
        "operator actions only. Switch the mode filter at the top to "
        "compare paper vs live equity growth side-by-side."
    )


main()
