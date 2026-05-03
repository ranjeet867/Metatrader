"""
test_strategy_library.py — the curated strategy library reads sweep_grid
output, ranks recommended-first, and produces a dataframe ready for the UI.
"""
from __future__ import annotations

import pytest

from core import strategy_library, edge_catalog


SAMPLE_MD = """\
# Grid

## ⭐ Edge candidates

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| USDJPY | D1 | ema_cross_9_20 | 29 | 1.42 | +0.109 | 13 | 3.74 | +0.947 |
| US100.cash | D1 | donchian_20 | 28 | 1.44 | +0.369 | 19 | 2.23 | +0.555 |
| EURUSD | M15 | rsi_30_70 | 62 | 1.20 | +0.117 | 41 | 1.30 | +0.165 |
| GBPUSD | D1 | ema_cross_9_20 | 26 | 1.10 | +0.020 | 13 | 2.18 | +0.607 |
| GBPJPY | D1 | ema_cross_12_26 | 23 | 1.54 | +0.300 | 8 | 3.64 | +0.935 |
| US100.cash | D1 | ema_cross_12_26 | 14 | 1.22 | +0.154 | 7 | 2.39 | +0.587 |
| GBPJPY | D1 | ema_pullback_20_50 | 67 | 0.76 | -0.122 | 32 | 1.70 | +0.248 |
| USDJPY | M15 | ema_cross_12_26 | 79 | 1.02 | +0.054 | 53 | 1.46 | +0.123 |
| GBPUSD | D1 | rsi_30_70 | 14 | 1.36 | +0.030 | 6 | 4.95 | +0.809 |
"""


@pytest.fixture
def patch_grid(tmp_path, monkeypatch):
    f = tmp_path / "grid.md"
    f.write_text(SAMPLE_MD)
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH", f)
    return f


def test_list_library_returns_recommended_first(patch_grid):
    lib = strategy_library.list_library()
    # First entries should all be recommended ⭐
    rec_block = []
    for e in lib:
        if not e.recommended:
            break
        rec_block.append(e)
    assert len(rec_block) >= 1
    assert all(e.recommended for e in rec_block)


def test_recommended_entries_link_to_real_edge_stats(patch_grid):
    lib = strategy_library.list_library()
    for e in lib:
        if e.recommended:
            assert e.edge is not None
            assert e.edge.test_pf > 0
            assert e.ticker == e.edge.ticker
            assert e.tf == e.edge.tf


def test_unique_slugs(patch_grid):
    lib = strategy_library.list_library()
    slugs = [e.slug for e in lib]
    assert len(slugs) == len(set(slugs))


def test_only_with_edge_filter_drops_non_survivors(patch_grid):
    lib_all = strategy_library.list_library()
    lib_edge = strategy_library.list_library(only_with_edge=True)
    # Recommended entries are ALWAYS included — even if their best edge
    # row didn't survive (the recommended ones are curated).
    rec_count_all = sum(1 for e in lib_all if e.recommended)
    rec_count_edge = sum(1 for e in lib_edge if e.recommended)
    assert rec_count_all == rec_count_edge
    # Non-recommended must all be survivors
    for e in lib_edge:
        if not e.recommended:
            assert e.has_edge


def test_to_dataframe_has_expected_columns(patch_grid):
    lib = strategy_library.list_library()
    df = strategy_library.to_dataframe(lib)
    expected = {"rec", "strategy", "ticker", "tf", "side",
                 "n_test", "PF_test", "R_test",
                 "PF_train", "R_train", "edge?", "why", "slug"}
    assert expected.issubset(df.columns)


def test_empty_catalog_yields_empty_library(monkeypatch, tmp_path):
    """Without grid_results.md the library is empty (recommended entries
    can't be backed by stats)."""
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH",
                          tmp_path / "absent.md")
    lib = strategy_library.list_library()
    assert lib == []


def test_recommended_for_known_ticker_tf_present(patch_grid):
    """The hard-coded portfolio includes USDJPY D1 ema_cross — verify it
    surfaces with the right stats."""
    lib = strategy_library.list_library()
    found = [e for e in lib
             if e.recommended and e.ticker == "USDJPY" and e.tf == "D1"
             and e.strategy.startswith("ema_cross")]
    assert len(found) == 1
    assert found[0].edge is not None
    assert found[0].edge.test_r > 0


def test_dataframe_emoji_marker_only_on_recommended(patch_grid):
    df = strategy_library.to_dataframe(strategy_library.list_library())
    rec_rows = df[df["rec"] == "⭐"]
    other_rows = df[df["rec"] != "⭐"]
    assert len(rec_rows) >= 1
    # Every "rec" cell is either ⭐ or empty — never anything else
    for v in df["rec"]:
        assert v in ("⭐", "")
