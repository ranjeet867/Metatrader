"""
test_journal_queries.py — pin the trade-journal queries that drive the
unified Trade Journal page (paper/live segregation, equity curves,
streaks, headline KPIs).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core import perf_query, storage


@pytest.fixture
def populated_db(tmp_path):
    db = tmp_path / "v2.db"
    storage.init_schema(db)
    seq = [
        # (mode, strat, ticker, tf, pnl, r, closed_utc)
        ("paper", "ema_cross", "EURUSD", "D1",  +200.0, +2.0,
         "2026-05-01T10:00:00+00:00"),
        ("paper", "ema_cross", "EURUSD", "D1",  -100.0, -1.0,
         "2026-05-01T15:00:00+00:00"),
        ("live",  "vol_breakout", "US100.cash", "D1", +500.0, +3.0,
         "2026-05-02T11:00:00+00:00"),
        ("live",  "vol_breakout", "US100.cash", "D1", -100.0, -1.0,
         "2026-05-02T14:00:00+00:00"),
        ("live",  "vol_breakout", "US100.cash", "D1", +250.0, +2.5,
         "2026-05-03T12:00:00+00:00"),
        ("backtest", "ema_cross", "EURUSD", "D1", +9999.0, +5.0,
         "2026-05-03T13:00:00+00:00"),  # should NOT appear in journal
    ]
    with sqlite3.connect(str(db)) as c:
        for i, (mode, strat, ticker, tf, pnl, r, closed) in enumerate(seq):
            c.execute(
                """INSERT INTO trades
                   (run_id, trade_idx, symbol, direction,
                    opened_at_utc, closed_at_utc,
                    entry_price, stop_price, target_price, exit_price,
                    lots, realized_pnl, r_multiple, close_reason,
                    mode, strategy, tf)
                   VALUES (?, ?, ?, 'LONG',
                           ?, ?,
                           100.0, 99.0, 102.0, 101.0,
                           1.0, ?, ?, 'target',
                           ?, ?, ?)""",
                ("run1", i, ticker,
                 "2026-05-01T09:00:00+00:00", closed,
                 pnl, r, mode, strat, tf),
            )
    return db


def test_journal_trades_excludes_backtest(populated_db):
    df = perf_query.journal_trades(populated_db)
    assert "backtest" not in df["mode"].unique()
    assert len(df) == 5      # 2 paper + 3 live


def test_journal_trades_filters_by_mode(populated_db):
    paper = perf_query.journal_trades(populated_db, modes=["paper"])
    live = perf_query.journal_trades(populated_db, modes=["live"])
    assert len(paper) == 2
    assert len(live) == 3
    assert paper["mode"].unique().tolist() == ["paper"]
    assert live["mode"].unique().tolist() == ["live"]


def test_journal_trades_sorted_newest_first(populated_db):
    df = perf_query.journal_trades(populated_db)
    closed = df["closed_at_utc"].tolist()
    assert closed == sorted(closed, reverse=True)


def test_journal_trades_filters_by_strategy(populated_db):
    df = perf_query.journal_trades(populated_db, strategy="vol_breakout")
    assert df["strategy"].unique().tolist() == ["vol_breakout"]
    assert len(df) == 3


def test_journal_trades_computes_duration(populated_db):
    df = perf_query.journal_trades(populated_db, modes=["paper"])
    # opened 09:00, closed 10:00 → 60 min ; opened 09:00, closed 15:00 → 360 min
    assert sorted(df["duration_min"].tolist()) == [60.0, 360.0]


def test_journal_trades_empty_when_db_missing(tmp_path):
    df = perf_query.journal_trades(tmp_path / "nope.db")
    assert df.empty


def test_daily_pnl_per_mode(populated_db):
    paper_daily = perf_query.daily_pnl(populated_db, mode="paper")
    assert len(paper_daily) == 1
    assert paper_daily.iloc[0]["pnl"] == pytest.approx(100.0)  # +200 - 100
    assert paper_daily.iloc[0]["n_trades"] == 2

    live_daily = perf_query.daily_pnl(populated_db, mode="live")
    assert len(live_daily) == 2     # 2 distinct UTC days
    assert live_daily["cum_pnl"].iloc[-1] == pytest.approx(650.0)


def test_streak_analysis_live(populated_db):
    s = perf_query.streak_analysis(populated_db, mode="live")
    # live trades chronologically: +500, -100, +250
    # streaks: win(1), loss(1), win(1) → max_win=1, max_loss=1
    # current = win streak of 1 (last trade was +250)
    assert s["max_win_streak"] == 1
    assert s["max_loss_streak"] == 1
    assert s["current_streak"] == 1
    assert s["current_kind"] == "winning"


def test_streak_analysis_consecutive_losses():
    """3 losses in a row should report max_loss_streak=3."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "v2.db"
        storage.init_schema(db)
        with sqlite3.connect(str(db)) as c:
            for i, pnl in enumerate([+50, -10, -10, -10, +5]):
                c.execute(
                    """INSERT INTO trades (run_id, trade_idx, symbol, direction,
                                            opened_at_utc, closed_at_utc,
                                            entry_price, stop_price, target_price,
                                            exit_price, lots, realized_pnl,
                                            r_multiple, close_reason, mode,
                                            strategy, tf)
                       VALUES ('r', ?, 'X', 'LONG',
                                '2026-05-03T09:00:00+00:00',
                                ?, 1.0, 0.5, 2.0, 1.0,
                                1.0, ?, 1.0, 'target',
                                'live', 's', 'D1')""",
                    (i, f"2026-05-03T1{i}:00:00+00:00", pnl),
                )
        s = perf_query.streak_analysis(db, mode="live")
        assert s["max_loss_streak"] == 3
        assert s["current_kind"] == "winning"
        assert s["current_streak"] == 1


def test_headline_kpis_paper(populated_db):
    k = perf_query.headline_kpis(populated_db, mode="paper",
                                    starting_balance=100_000.0)
    assert k["n_trades"] == 2
    assert k["total_pnl"] == pytest.approx(100.0)
    assert k["win_rate_pct"] == pytest.approx(50.0)
    assert k["current_equity"] == pytest.approx(100_100.0)


def test_headline_kpis_empty(tmp_path):
    db = tmp_path / "empty.db"
    storage.init_schema(db)
    k = perf_query.headline_kpis(db, mode="live")
    assert k["n_trades"] == 0
    assert k["total_pnl"] == 0.0
    assert k["current_equity"] == 100_000.0
