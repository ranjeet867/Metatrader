"""
test_paper_loop.py — multi-strategy paper loop with mocked candle fetcher.

Tests do NOT spin up the real thread; they call .tick() directly so
behaviour is deterministic.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import storage
from core.paper_loop import PaperLoop, PaperStrategyConfig
from core.strategy import Signal


def _tmp_db() -> Path:
    return Path(tempfile.mkstemp(suffix=".db")[1])


def _h1(start="2026-05-04T00:00:00Z", n=20) -> pd.DataFrame:
    times = pd.date_range(start, periods=n, freq="h", tz="UTC")
    closes = 100.0 + np.arange(n, dtype=float) * 0.1
    return pd.DataFrame({
        "time": times,
        "open":  closes - 0.05,
        "high":  closes + 0.05,
        "low":   closes - 0.05,
        "close": closes,
        "volume": np.full(n, 1000.0),
    })


class _StubStrategy:
    def __init__(self, name: str, sig_at: int, candles: pd.DataFrame):
        self.name = name
        self._sig_at = sig_at
        self._candles = candles

    def signals(self, view: pd.DataFrame):
        if self._sig_at >= len(view):
            return []
        entry = float(view["close"].iloc[self._sig_at])
        return [Signal(
            bar_idx=self._sig_at, direction="LONG",
            entry_price=entry, stop_price=entry - 1.0,
            target_price=entry + 2.0,
        )]


def test_single_config_open_persists_to_db():
    candles = _h1(n=12)
    fetched = []

    def fetcher(sym, tf, n):
        fetched.append((sym, tf, n))
        return candles

    cfg = PaperStrategyConfig(
        strategy=_StubStrategy("stub", sig_at=11, candles=candles),
        symbol="US100.cash", tf="H1",
        lots=1.0, money_per_unit_price=1.0,
        name_for_journal="stub_us100",
    )
    loop = PaperLoop(configs=[cfg], candle_fetcher=fetcher,
                      db_path=_tmp_db())
    events = loop.tick()
    assert any(e.kind == "open" for e in events)
    # Heartbeat updated
    with storage.connect(loop.db_path) as c:
        row = c.execute(
            "SELECT heartbeat_at_utc FROM paper_runs WHERE run_id=?",
            (loop.run_id,),
        ).fetchone()
    assert row[0] is not None


def test_close_persists_with_paper_mode():
    candles = _h1(n=12)
    # First tick: open at bar 11. Add one more bar where the position
    # hits target.
    candles_more = _h1(n=13)
    candles_more.loc[12, "high"] = 200.0   # target hit

    state = {"call": 0}

    def fetcher(sym, tf, n):
        state["call"] += 1
        return candles if state["call"] == 1 else candles_more

    cfg = PaperStrategyConfig(
        strategy=_StubStrategy("stub", sig_at=11, candles=candles),
        symbol="X", tf="H1", lots=1.0, money_per_unit_price=1.0,
    )
    loop = PaperLoop(configs=[cfg], candle_fetcher=fetcher,
                      db_path=_tmp_db())
    loop.tick()      # open
    loop.tick()      # close (target)
    with storage.connect(loop.db_path) as c:
        rows = c.execute(
            "SELECT mode, close_reason, realized_pnl FROM trades "
            "WHERE run_id=? AND mode='paper' ORDER BY trade_idx",
            (loop.run_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "paper"
    assert rows[0][1] == "target"
    assert rows[0][2] > 0


def test_bridge_timeout_does_not_crash_loop():
    """If the candle fetcher raises, the loop logs an error event but
    continues. Subsequent ticks still work."""
    state = {"call": 0}
    candles = _h1(n=12)

    def fetcher(sym, tf, n):
        state["call"] += 1
        if state["call"] == 1:
            raise TimeoutError("bridge down")
        return candles

    cfg = PaperStrategyConfig(
        strategy=_StubStrategy("stub", sig_at=11, candles=candles),
        symbol="X", tf="H1", lots=1.0, money_per_unit_price=1.0,
    )
    loop = PaperLoop(configs=[cfg], candle_fetcher=fetcher,
                      db_path=_tmp_db())
    events1 = loop.tick()     # error
    assert any(e.kind == "error" for e in events1)
    events2 = loop.tick()     # works — opens position
    assert any(e.kind == "open" for e in events2)


def test_multi_strategy_no_cross_contamination():
    """3 configs on 3 different symbols — each opens independently."""
    candles_a = _h1(n=12)
    candles_b = _h1(n=12, start="2026-05-04T01:00:00Z")
    candles_c = _h1(n=12, start="2026-05-04T02:00:00Z")
    fetched_for = {"A": candles_a, "B": candles_b, "C": candles_c}

    def fetcher(sym, tf, n):
        return fetched_for[sym]

    configs = [
        PaperStrategyConfig(
            strategy=_StubStrategy(f"stub_{s}", sig_at=11, candles=fetched_for[s]),
            symbol=s, tf="H1", lots=1.0, money_per_unit_price=1.0,
            name_for_journal=f"stub_{s}",
        )
        for s in ("A", "B", "C")
    ]
    loop = PaperLoop(configs=configs, candle_fetcher=fetcher,
                      db_path=_tmp_db())
    loop.tick()
    # All three should have opened
    assert all(loop.get_executor(c.name_for_journal).n_open == 1
                 for c in configs)


def test_no_double_open_on_same_bar():
    """If tick() is called twice on the same view, the second call must NOT
    re-open (idempotency_key dedup)."""
    candles = _h1(n=12)

    def fetcher(sym, tf, n):
        return candles

    cfg = PaperStrategyConfig(
        strategy=_StubStrategy("stub", sig_at=11, candles=candles),
        symbol="X", tf="H1", lots=1.0, money_per_unit_price=1.0,
    )
    loop = PaperLoop(configs=[cfg], candle_fetcher=fetcher,
                      db_path=_tmp_db())
    e1 = loop.tick()
    e2 = loop.tick()
    assert sum(1 for e in e1 if e.kind == "open") == 1
    # Second tick: last_bar_seen short-circuits → no events
    assert sum(1 for e in e2 if e.kind == "open") == 0
    assert loop.get_executor(cfg.name_for_journal).n_open == 1
