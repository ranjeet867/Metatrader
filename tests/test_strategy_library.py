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
    # Also isolate from auto-discovery from data/v2.db so tests see
    # only the SAMPLE_MD content (not real backtest runs).
    monkeypatch.setattr(edge_catalog, "DEFAULT_DB_PATH",
                          tmp_path / "absent.db")
    return f


def test_is_starred_requires_curated_and_safe_and_sample():
    """⭐ only when (recommended AND deploy_safe AND n_test ≥ 20)."""
    es_safe_big = edge_catalog.EdgeStat(
        ticker="X", tf="D1", strategy="s",
        n_train=30, train_pf=1.5, train_r=0.2,
        n_test=25, test_pf=1.5, test_r=0.2,
        hard_gate_failed=(),
    )
    es_safe_small = edge_catalog.EdgeStat(
        ticker="X", tf="D1", strategy="s",
        n_train=20, train_pf=1.5, train_r=0.2,
        n_test=16, test_pf=1.5, test_r=0.2,
        hard_gate_failed=(),
    )
    es_unsafe = edge_catalog.EdgeStat(
        ticker="X", tf="D1", strategy="s",
        n_train=30, train_pf=1.5, train_r=0.2,
        n_test=25, test_pf=1.5, test_r=0.2,
        hard_gate_failed=("Recovery: not yet",),
    )

    e_curated_safe_big = strategy_library.LibraryEntry(
        strategy="s", ticker="X", tf="D1", long_only=True,
        recommended=True, edge=es_safe_big,
    )
    assert e_curated_safe_big.is_starred is True

    # Edge case from the user's screenshot: curated, deploy_safe by
    # gate, but sample size too small for stable verdict (16 < 20).
    # Pre-fix this would star — now it doesn't, avoiding the
    # "⭐ but FAILS gates" UX inconsistency on the deep-link.
    e_curated_safe_small = strategy_library.LibraryEntry(
        strategy="s", ticker="X", tf="D1", long_only=True,
        recommended=True, edge=es_safe_small,
    )
    assert e_curated_safe_small.is_starred is False

    e_curated_unsafe = strategy_library.LibraryEntry(
        strategy="s", ticker="X", tf="D1", long_only=True,
        recommended=True, edge=es_unsafe,
    )
    assert e_curated_unsafe.is_starred is False

    e_uncurated = strategy_library.LibraryEntry(
        strategy="s", ticker="X", tf="D1", long_only=True,
        recommended=False, edge=es_safe_big,
    )
    assert e_uncurated.is_starred is False

    e_no_edge = strategy_library.LibraryEntry(
        strategy="s", ticker="X", tf="D1", long_only=True,
        recommended=True, edge=None,
    )
    assert e_no_edge.is_starred is False


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
    """The library DataFrame must surface the AlgoTest-style columns.

    PF_train / R_train were dropped after the optimizer wide-format
    migration — those numbers were always equal to PF_test / R_test in
    optimizer rows anyway. The new $-amount columns replace them.
    """
    lib = strategy_library.list_library()
    df = strategy_library.to_dataframe(lib)
    expected = {"rec", "confidence", "strategy", "ticker", "tf", "side",
                 "R:R", "n_test", "PF_test", "R_test",
                 "win%", "netPnL$", "maxDD%", "maxDD$", "DDdays",
                 "recovD", "rr", "avgWin$", "avgLoss$", "P(pass)",
                 "edge?", "why", "slug"}
    assert expected.issubset(df.columns)


def test_empty_catalog_yields_empty_library(monkeypatch, tmp_path):
    """Without grid_results.md the library is empty (recommended entries
    can't be backed by stats)."""
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH",
                          tmp_path / "absent.md")
    monkeypatch.setattr(edge_catalog, "DEFAULT_DB_PATH",
                          tmp_path / "absent.db")
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


def test_dataframe_emoji_marker_three_state(patch_grid):
    """The `rec` column has three valid states:
      ⭐  — curated AND deploy_safe AND n_test ≥ 20 (truly recommended)
      📍  — curated but currently failing a hard gate (legacy / borderline)
      ""  — uncurated cell from the broader sweep
    Pre-fix this was a 2-state column (⭐ or "") which let cells with
    16 OOS trades get ⭐ even though the Backtest deep-link would
    flag them as failing — see is_starred docstring.
    """
    df = strategy_library.to_dataframe(strategy_library.list_library())
    valid = {"⭐", "📍", ""}
    for v in df["rec"]:
        assert v in valid, f"unexpected rec value: {v!r}"
    # SAMPLE_MD has small n_test values (8–13), so most curated rows
    # land in the 📍 bucket rather than ⭐. That's fine — we just need
    # SOME curated row visible.
    n_curated = (df["rec"] == "⭐").sum() + (df["rec"] == "📍").sum()
    assert n_curated >= 1
