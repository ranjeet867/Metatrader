"""
test_edge_catalog.py — parses sweep_grid markdown output, fetches per-cell
edge stats. Tolerant of missing / malformed file (returns empty, never raises).
"""
from __future__ import annotations

import pytest

from core import edge_catalog
from core.edge_catalog import EdgeStat


SAMPLE_MD = """\
# Grid Sweep Results

- Run config: comm=$3.0  slip=0.1×ATR

## ⭐ Edge candidates

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| USDJPY | D1 | ema_cross_9_20 | 29 | 1.42 | +0.109 | 13 | 3.74 | +0.947 |
| US100.cash | D1 | donchian_20 | 28 | 1.44 | +0.369 | 19 | 2.23 | +0.555 |
| EURUSD | M15 | rsi_30_70 | 62 | 1.20 | +0.117 | 41 | 1.30 | +0.165 |

## Top 30 cells by test_R

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| USDJPY | D1 | ema_cross_9_20 | 29 | 1.42 | +0.109 | 13 | 3.74 | +0.947 |
| GBPUSD | D1 | rsi_30_70 | 14 | 1.36 | +0.030 | 6 | 4.95 | +0.809 |
| US100.cash | M15 | donchian_20 | 118 | 0.81 | -0.122 | 84 | 1.32 | +0.252 |
"""


def test_load_catalog_returns_empty_when_missing(tmp_path):
    assert edge_catalog.load_catalog(tmp_path / "nope.md") == {}


def test_load_catalog_returns_empty_when_empty_file(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("# nothing here\n")
    assert edge_catalog.load_catalog(f) == {}


def test_load_catalog_parses_table(tmp_path):
    f = tmp_path / "grid.md"
    f.write_text(SAMPLE_MD)
    cat = edge_catalog.load_catalog(f)
    # 4 unique (ticker, tf) cells expected
    assert ("USDJPY", "D1") in cat
    assert ("US100.cash", "D1") in cat
    assert ("EURUSD", "M15") in cat
    assert ("GBPUSD", "D1") in cat
    assert ("US100.cash", "M15") in cat
    # Dedupes — USDJPY D1 ema_cross_9_20 appears in both tables but counts once
    rows = cat[("USDJPY", "D1")]
    assert len(rows) == 1
    r = rows[0]
    assert isinstance(r, EdgeStat)
    assert r.strategy == "ema_cross_9_20"
    assert r.train_pf == pytest.approx(1.42)
    assert r.test_pf == pytest.approx(3.74)
    assert r.test_r == pytest.approx(0.947)
    assert r.n_test == 13


def test_best_for_picks_highest_test_r(tmp_path):
    f = tmp_path / "grid.md"
    f.write_text(SAMPLE_MD)
    # GBPUSD D1: only rsi_30_70 (test_r=0.809)
    b = edge_catalog.best_for("GBPUSD", "D1", path=f)
    assert b.strategy == "rsi_30_70"
    assert b.test_r == pytest.approx(0.809)


def test_best_for_strategy_prefix_filter(tmp_path):
    f = tmp_path / "grid.md"
    f.write_text(SAMPLE_MD)
    # US100.cash D1: only donchian (test_r=0.555). Filter for ema → None.
    assert edge_catalog.best_for("US100.cash", "D1",
                                  "ema_cross", path=f) is None
    # Filter for donchian → finds it.
    b = edge_catalog.best_for("US100.cash", "D1", "donchian", path=f)
    assert b.strategy == "donchian_20"


def test_is_survivor_property():
    s = EdgeStat(ticker="X", tf="D1", strategy="s",
                 n_train=10, train_pf=1.5, train_r=0.2,
                 n_test=10, test_pf=1.8, test_r=0.3)
    assert s.is_survivor

    # n_test < 5 → not a survivor
    s = EdgeStat(ticker="X", tf="D1", strategy="s",
                 n_train=10, train_pf=2.0, train_r=0.5,
                 n_test=4,  test_pf=2.0, test_r=0.5)
    assert not s.is_survivor

    # train negative → not a survivor
    s = EdgeStat(ticker="X", tf="D1", strategy="s",
                 n_train=10, train_pf=0.8, train_r=-0.1,
                 n_test=10, test_pf=2.0, test_r=0.4)
    assert not s.is_survivor


def test_total_trades():
    s = EdgeStat(ticker="X", tf="D1", strategy="s",
                 n_train=20, train_pf=1.0, train_r=0.0,
                 n_test=8, test_pf=1.0, test_r=0.0)
    assert s.total_trades == 28


def test_load_catalog_survives_malformed_rows(tmp_path):
    f = tmp_path / "malformed.md"
    f.write_text("""\
| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| USDJPY | D1 | only_three_cols |
| USDJPY | D1 | good | 10 | 1.5 | +0.2 | 5 | 1.6 | +0.3 |
""")
    cat = edge_catalog.load_catalog(f)
    # Only the well-formed row makes it
    assert sum(len(v) for v in cat.values()) == 1
    assert cat[("USDJPY", "D1")][0].strategy == "good"
