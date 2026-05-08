"""
perf_query.py — read-only analytics over the trades journal in v2.db.

Splits paper from live so the dashboard can answer "which strategy made
how much in paper" vs "which strategy made how much in live" without
mixing the two. All functions take a db_path so tests can swap in a
fixture DB.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Explicit __all__ so the Trade Journal page (and any future caller) can
# fail fast at import time rather than at attribute-access time. Add new
# functions HERE when you extend the module.
__all__ = [
    "StrategyPnL",
    "per_strategy_pnl",
    "equity_curve",
    "overall_summary",
    "journal_trades",
    "daily_pnl",
    "streak_analysis",
    "headline_kpis",
]


@dataclass(frozen=True)
class StrategyPnL:
    strategy: str
    ticker: str
    tf: str
    mode: str           # 'paper' | 'live'
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate_pct: float
    total_pnl: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    last_trade_utc: str | None

    @property
    def slug(self) -> str:
        return f"{self.strategy}__{self.ticker}__{self.tf}__{self.mode}"


def _connect(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    if not p.exists():
        # Returning an empty in-memory DB lets the dashboard show empty
        # tables instead of crashing on first run.
        return sqlite3.connect(":memory:")
    return sqlite3.connect(str(p))


def per_strategy_pnl(db_path: str | Path, *, mode: str
                      ) -> list[StrategyPnL]:
    """Group trades by (strategy, ticker, tf) for a given mode and
    aggregate P&L.

    Returns a list sorted by total_pnl descending.
    """
    if mode not in {"paper", "live", "backtest"}:
        raise ValueError(f"mode must be paper/live/backtest, got {mode!r}")
    conn = _connect(db_path)
    try:
        try:
            df = pd.read_sql_query(
                """
                SELECT strategy, symbol AS ticker, tf,
                       realized_pnl, closed_at_utc
                FROM trades
                WHERE mode = ?
                  AND strategy IS NOT NULL
                  AND realized_pnl IS NOT NULL
                """,
                conn, params=(mode,),
            )
        except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
            return []
    finally:
        conn.close()
    if df.empty:
        return []

    rows: list[StrategyPnL] = []
    grp = df.groupby(["strategy", "ticker", "tf"], dropna=False)
    for (strat, tic, tf), g in grp:
        wins = g[g["realized_pnl"] > 0]["realized_pnl"]
        losses = g[g["realized_pnl"] < 0]["realized_pnl"]
        n = len(g)
        n_w = len(wins)
        n_l = len(losses)
        rows.append(StrategyPnL(
            strategy=strat or "unknown",
            ticker=tic or "unknown",
            tf=tf or "?",
            mode=mode,
            n_trades=n,
            n_wins=n_w,
            n_losses=n_l,
            win_rate_pct=(n_w / n * 100.0) if n else 0.0,
            total_pnl=float(g["realized_pnl"].sum()),
            avg_win=float(wins.mean()) if n_w else 0.0,
            avg_loss=float(losses.mean()) if n_l else 0.0,
            largest_win=float(wins.max()) if n_w else 0.0,
            largest_loss=float(losses.min()) if n_l else 0.0,
            last_trade_utc=(str(g["closed_at_utc"].max())
                            if g["closed_at_utc"].notna().any() else None),
        ))
    rows.sort(key=lambda r: -r.total_pnl)
    return rows


def equity_curve(db_path: str | Path, *, mode: str,
                  strategy: str | None = None,
                  ticker: str | None = None,
                  starting_balance: float = 100_000.0
                  ) -> pd.DataFrame:
    """Return a DataFrame [time_utc, equity, cum_pnl] for the trades
    journal filtered by mode (and optionally strategy/ticker).

    The 'equity' starts at starting_balance and steps up/down at each
    closed_at_utc by the trade's realized_pnl. Good enough for a
    visualization — not the bar-by-bar mark-to-market curve from
    backtest.run_backtest.
    """
    conn = _connect(db_path)
    where = ["mode = ?", "realized_pnl IS NOT NULL", "closed_at_utc IS NOT NULL"]
    params: list = [mode]
    if strategy:
        where.append("strategy = ?"); params.append(strategy)
    if ticker:
        where.append("symbol = ?"); params.append(ticker)
    sql = f"""
        SELECT closed_at_utc AS time_utc, realized_pnl
        FROM trades
        WHERE {' AND '.join(where)}
        ORDER BY closed_at_utc
    """
    try:
        df = pd.read_sql_query(sql, conn, params=params)
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        df = pd.DataFrame(columns=["time_utc", "realized_pnl"])
    finally:
        conn.close()
    if df.empty:
        return pd.DataFrame({"time_utc": [], "equity": [], "cum_pnl": []})
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True, errors="coerce")
    df = df.dropna(subset=["time_utc"]).sort_values("time_utc")
    df["cum_pnl"] = df["realized_pnl"].cumsum()
    df["equity"] = starting_balance + df["cum_pnl"]
    return df.reset_index(drop=True)


def overall_summary(db_path: str | Path, *, mode: str) -> dict:
    """Headline KPIs for the mode: total P&L, n_trades, win rate, n_strategies."""
    rows = per_strategy_pnl(db_path, mode=mode)
    if not rows:
        return {"total_pnl": 0.0, "n_trades": 0, "win_rate_pct": 0.0,
                "n_strategies": 0, "n_winners": 0, "n_losers": 0}
    total_pnl = sum(r.total_pnl for r in rows)
    n_trades = sum(r.n_trades for r in rows)
    n_wins = sum(r.n_wins for r in rows)
    n_winners = sum(1 for r in rows if r.total_pnl > 0)
    n_losers = sum(1 for r in rows if r.total_pnl < 0)
    return {
        "total_pnl": total_pnl,
        "n_trades": n_trades,
        "win_rate_pct": (n_wins / n_trades * 100.0) if n_trades else 0.0,
        "n_strategies": len(rows),
        "n_winners": n_winners,
        "n_losers": n_losers,
    }


def journal_trades(
    db_path: str | Path,
    *,
    modes: list[str] | None = None,        # default: ["paper", "live"]
    strategy: str | None = None,
    ticker: str | None = None,
    tf: str | None = None,
    since_utc: str | None = None,           # ISO-8601 lower bound
    until_utc: str | None = None,
    limit: int = 5_000,
) -> pd.DataFrame:
    """Return a journal of CLOSED trades as a DataFrame, sorted newest
    first. Columns:
      run_id, mode, strategy, symbol, tf, direction,
      opened_at_utc, closed_at_utc, duration_min,
      entry_price, stop_price, target_price, exit_price,
      lots, realized_pnl, r_multiple, close_reason

    Filters compose with AND. The default modes filter ['paper','live']
    keeps backtest replay trades out of the journal — those belong to
    the Backtest page.
    """
    modes = list(modes) if modes else ["paper", "live"]
    if not modes:
        return pd.DataFrame()
    where: list[str] = ["closed_at_utc IS NOT NULL",
                          "realized_pnl IS NOT NULL"]
    where.append(f"mode IN ({', '.join('?' * len(modes))})")
    params: list = list(modes)
    if strategy:
        where.append("strategy = ?"); params.append(strategy)
    if ticker:
        where.append("symbol = ?"); params.append(ticker)
    if tf:
        where.append("tf = ?"); params.append(tf)
    if since_utc:
        where.append("closed_at_utc >= ?"); params.append(since_utc)
    if until_utc:
        where.append("closed_at_utc <= ?"); params.append(until_utc)
    sql = f"""
        SELECT run_id, mode, strategy, symbol, tf, direction,
               opened_at_utc, closed_at_utc,
               entry_price, stop_price, target_price, exit_price,
               lots, realized_pnl, r_multiple, close_reason
        FROM trades
        WHERE {' AND '.join(where)}
        ORDER BY closed_at_utc DESC, trade_idx DESC
        LIMIT {int(limit)}
    """
    conn = _connect(db_path)
    try:
        df = pd.read_sql_query(sql, conn, params=params)
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        return pd.DataFrame()
    finally:
        conn.close()
    if df.empty:
        return df
    df["opened_at_utc"] = pd.to_datetime(df["opened_at_utc"], utc=True,
                                            errors="coerce")
    df["closed_at_utc"] = pd.to_datetime(df["closed_at_utc"], utc=True,
                                            errors="coerce")
    df["duration_min"] = (
        (df["closed_at_utc"] - df["opened_at_utc"]).dt.total_seconds() / 60.0
    ).round(1)
    return df


def daily_pnl(
    db_path: str | Path,
    *,
    mode: str,
    strategy: str | None = None,
) -> pd.DataFrame:
    """Sum realized_pnl per UTC calendar day for the given mode.

    Returns columns [date, pnl, n_trades, cum_pnl] sorted by date.
    """
    df = journal_trades(db_path, modes=[mode], strategy=strategy)
    if df.empty:
        return pd.DataFrame(columns=["date", "pnl", "n_trades", "cum_pnl"])
    df["date"] = df["closed_at_utc"].dt.tz_convert("UTC").dt.date
    grp = df.groupby("date").agg(
        pnl=("realized_pnl", "sum"),
        n_trades=("realized_pnl", "size"),
    ).reset_index()
    grp = grp.sort_values("date").reset_index(drop=True)
    grp["cum_pnl"] = grp["pnl"].cumsum()
    return grp


def streak_analysis(
    db_path: str | Path,
    *,
    mode: str,
) -> dict:
    """Win/loss streaks, current streak, biggest streaks both ways."""
    df = journal_trades(db_path, modes=[mode]).sort_values("closed_at_utc")
    if df.empty:
        return {
            "current_streak": 0, "current_kind": "—",
            "max_win_streak": 0, "max_loss_streak": 0,
            "n_winners": 0, "n_losers": 0,
        }
    pnls = df["realized_pnl"].tolist()
    longest_win = longest_loss = 0
    cur_win = cur_loss = 0
    for p in pnls:
        if p > 0:
            cur_win += 1
            cur_loss = 0
            longest_win = max(longest_win, cur_win)
        elif p < 0:
            cur_loss += 1
            cur_win = 0
            longest_loss = max(longest_loss, cur_loss)
        else:
            cur_win = cur_loss = 0
    last_pnl = pnls[-1]
    if last_pnl > 0:
        current = cur_win
        kind = "winning"
    elif last_pnl < 0:
        current = cur_loss
        kind = "losing"
    else:
        current = 0
        kind = "—"
    return {
        "current_streak": current,
        "current_kind": kind,
        "max_win_streak": longest_win,
        "max_loss_streak": longest_loss,
        "n_winners": int((df["realized_pnl"] > 0).sum()),
        "n_losers": int((df["realized_pnl"] < 0).sum()),
    }


def headline_kpis(
    db_path: str | Path,
    *,
    mode: str,
    starting_balance: float = 100_000.0,
) -> dict:
    """Single-call headline summary for the journal page header."""
    df = journal_trades(db_path, modes=[mode])
    if df.empty:
        return {
            "n_trades": 0, "total_pnl": 0.0, "win_rate_pct": 0.0,
            "avg_r": 0.0, "expectancy_dollars": 0.0,
            "max_dd_dollars": 0.0, "max_dd_pct": 0.0,
            "current_streak": 0, "current_kind": "—",
            "best_day": 0.0, "worst_day": 0.0,
            "starting_balance": starting_balance,
            "current_equity": starting_balance,
        }
    n = len(df)
    total = float(df["realized_pnl"].sum())
    wins = df[df["realized_pnl"] > 0]
    n_wins = len(wins)
    avg_r = (float(df["r_multiple"].mean())
              if df["r_multiple"].notna().any() else 0.0)
    expectancy = total / n if n else 0.0
    # DD on the running-cum curve
    df_sorted = df.sort_values("closed_at_utc")
    cum = df_sorted["realized_pnl"].cumsum().to_numpy()
    if len(cum):
        peaks = []
        peak = 0.0
        max_dd = 0.0
        for v in cum:
            if v > peak:
                peak = v
            max_dd = min(max_dd, v - peak)
        max_dd_dollars = abs(max_dd)
    else:
        max_dd_dollars = 0.0
    max_dd_pct = (max_dd_dollars / starting_balance * 100.0
                    if starting_balance else 0.0)
    streak = streak_analysis(db_path, mode=mode)
    daily = daily_pnl(db_path, mode=mode)
    best_day = float(daily["pnl"].max()) if not daily.empty else 0.0
    worst_day = float(daily["pnl"].min()) if not daily.empty else 0.0
    return {
        "n_trades": n,
        "total_pnl": total,
        "win_rate_pct": (n_wins / n * 100.0) if n else 0.0,
        "avg_r": avg_r,
        "expectancy_dollars": expectancy,
        "max_dd_dollars": max_dd_dollars,
        "max_dd_pct": max_dd_pct,
        "current_streak": streak["current_streak"],
        "current_kind": streak["current_kind"],
        "best_day": best_day,
        "worst_day": worst_day,
        "starting_balance": starting_balance,
        "current_equity": starting_balance + total,
    }
