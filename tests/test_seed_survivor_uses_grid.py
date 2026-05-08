"""
test_seed_survivor_uses_grid.py — seed_survivor_deployments now uses the
recommended portfolio from core.strategy_library, not the legacy
vol_breakout list. When grid_results.md is missing, it falls back to a
static survivor list whose entries can actually be backtested.
"""
from __future__ import annotations

import pytest

from core import account_manager, deployment as dep_mod
from core import edge_catalog


SAMPLE_GRID = """\
| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| USDJPY | D1 | ema_cross_9_20 | 29 | 1.42 | +0.109 | 13 | 3.74 | +0.947 |
| GBPJPY | D1 | ema_cross_12_26 | 23 | 1.54 | +0.300 | 8 | 3.64 | +0.935 |
| GBPUSD | D1 | ema_cross_9_20 | 26 | 1.10 | +0.020 | 13 | 2.18 | +0.607 |
| US100.cash | D1 | donchian_20 | 28 | 1.44 | +0.369 | 19 | 2.23 | +0.555 |
| US100.cash | D1 | ema_cross_12_26 | 14 | 1.22 | +0.154 | 7 | 2.39 | +0.587 |
| GBPUSD | D1 | rsi_30_70 | 14 | 1.36 | +0.030 | 6 | 4.95 | +0.809 |
| GBPJPY | D1 | ema_pullback_20_50 | 67 | 0.76 | -0.122 | 32 | 1.70 | +0.248 |
| EURUSD | M15 | rsi_30_70 | 62 | 1.20 | +0.117 | 41 | 1.30 | +0.165 |
| USDJPY | M15 | ema_cross_12_26 | 79 | 1.02 | +0.054 | 53 | 1.46 | +0.123 |
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(account_manager, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(account_manager, "ACCOUNTS_REGISTRY",
                          tmp_path / "data" / "accounts.json")
    monkeypatch.setattr(account_manager, "ACCOUNTS_DIR",
                          tmp_path / "data" / "accounts")
    monkeypatch.setattr(account_manager, "LEGACY_DB_PATH",
                          tmp_path / "data" / "v2.db")
    monkeypatch.setattr(account_manager, "EMERGENCY_STOP_FILE",
                          tmp_path / "data" / "EMERGENCY_STOP")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)


def test_seeds_use_grid_survivors_when_catalog_present(monkeypatch, tmp_path):
    grid = tmp_path / "grid.md"
    grid.write_text(SAMPLE_GRID)
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH", grid)

    account_manager.add_account(login=5031019095, alias="X")
    deps = dep_mod.seed_survivor_deployments(5031019095)

    assert len(deps) >= 4
    # Every seeded deployment should have a strategy that exists in the
    # catalog, NOT vol_breakout.
    for d in deps:
        assert d.strategy != "vol_breakout"
        # The strategy name should match a surviving cell on (ticker, tf)
        # — verified by being parseable from the catalog
        cell = edge_catalog.best_for(d.ticker, d.tf, d.strategy.split("_")[0],
                                       path=grid)
        assert cell is not None


def test_seeds_idempotent(monkeypatch, tmp_path):
    grid = tmp_path / "grid.md"
    grid.write_text(SAMPLE_GRID)
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH", grid)

    account_manager.add_account(login=42, alias="X")
    a = dep_mod.seed_survivor_deployments(42)
    b = dep_mod.seed_survivor_deployments(42)
    assert {d.deployment_id for d in a} == {d.deployment_id for d in b}


def test_seeds_carry_notes(monkeypatch, tmp_path):
    grid = tmp_path / "grid.md"
    grid.write_text(SAMPLE_GRID)
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH", grid)
    account_manager.add_account(login=42, alias="X")
    deps = dep_mod.seed_survivor_deployments(42)
    notes_present = sum(1 for d in deps if d.notes)
    # The library entries have `why` strings — at least some should land
    assert notes_present >= 1


def test_static_fallback_when_catalog_missing(monkeypatch, tmp_path):
    """When grid_results.md is absent, we still seed something sensible
    (not vol_breakout) so the cards have stats they can actually reference."""
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH",
                          tmp_path / "absent.md")
    # Also isolate from the auto-discover-from-v2.db path added later —
    # otherwise the real data/v2.db backfills the catalog and the
    # "missing" fallback never fires.
    monkeypatch.setattr(edge_catalog, "DEFAULT_DB_PATH",
                          tmp_path / "absent.db")
    account_manager.add_account(login=42, alias="X")
    deps = dep_mod.seed_survivor_deployments(42)
    # Fallback is the 4-row static list
    assert len(deps) == 4
    # No vol_breakout
    assert all(d.strategy != "vol_breakout" for d in deps)
    # Should be a mix of ema_cross and donchian on real tickers
    strategies = {d.strategy for d in deps}
    assert "ema_cross_9_20" in strategies
    assert "donchian_20" in strategies


def test_no_seed_deployment_uses_vol_breakout(monkeypatch, tmp_path):
    """Regression: vol_breakout is not in grid_results.md so cards used to
    show 'no backtest edge cached'. Make sure the new seed never picks it."""
    grid = tmp_path / "grid.md"
    grid.write_text(SAMPLE_GRID)
    monkeypatch.setattr(edge_catalog, "DEFAULT_GRID_PATH", grid)
    account_manager.add_account(login=42, alias="X")
    deps = dep_mod.seed_survivor_deployments(42)
    for d in deps:
        assert "vol_breakout" not in d.strategy
        assert "vol_breakout" not in d.deployment_id
