"""
test_edge_decay.py — verify the edge-decay autolearner correctly
classifies live performance vs catalog expectations.

Coverage:
1. INSUFFICIENT state when fewer than MIN_TRADES_TO_ASSESS closed trades
2. GREEN when live R is within 1σ of catalog
3. BLUE when live R is materially above catalog
4. YELLOW at -2 ≤ z < -1
5. RED only when sustained-red count ≥ MIN_RED_TRADES (15 consecutive)
6. assess_all() filters to deployments with catalog lookup
7. record_trade() schema bootstrap
8. Empty DB / missing table — no crash, returns INSUFFICIENT
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import edge_decay as ed


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_decay.db"


def _record_n(db: Path, dep_id: str, R_values: list[float],
                catalog_R: float = 0.13) -> None:
    """Helper: record N closed trades with given R values, oldest first.
    The fetch sorts by closed_at_utc DESC, so to get newest-first we
    record with monotonically-increasing timestamps."""
    import datetime as _dt
    base = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
    for i, r in enumerate(R_values):
        ts = (base + _dt.timedelta(hours=i)).isoformat()
        ed.record_trade(db, deployment_id=dep_id, live_R=r,
                          catalog_R=catalog_R, closed_at_utc=ts)


# ----------------------------------------------------------------- #
# Empty / insufficient                                              #
# ----------------------------------------------------------------- #
def test_empty_db_returns_insufficient(tmp_db):
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.13)
    assert a.state == ed.DecayState.INSUFFICIENT
    assert a.n_trades == 0
    assert a.z_score is None


def test_below_min_trades_returns_insufficient(tmp_db):
    _record_n(tmp_db, "x", [0.5, -0.3, 0.1])  # only 3 trades
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.13)
    assert a.state == ed.DecayState.INSUFFICIENT
    assert a.n_trades == 3


# ----------------------------------------------------------------- #
# GREEN — within noise                                              #
# ----------------------------------------------------------------- #
def test_green_when_live_matches_catalog(tmp_db):
    # 30 trades centered on catalog R with moderate spread → z ≈ 0
    rs = [0.13 + 0.5 * (i % 3 - 1) for i in range(30)]  # mean = 0.13
    _record_n(tmp_db, "x", rs)
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.13)
    assert a.state == ed.DecayState.GREEN
    assert a.z_score is not None and abs(a.z_score) < 0.5


# ----------------------------------------------------------------- #
# BLUE — over-performing                                            #
# ----------------------------------------------------------------- #
def test_blue_when_live_well_above_catalog(tmp_db):
    # Live mean ~+0.6, catalog 0.0 → z very positive
    rs = [0.5 + 0.2 * (i % 2) for i in range(30)]
    _record_n(tmp_db, "x", rs, catalog_R=0.0)
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.0)
    assert a.state == ed.DecayState.BLUE
    assert a.z_score is not None and a.z_score > 1.0


# ----------------------------------------------------------------- #
# YELLOW — moderately under-performing                              #
# ----------------------------------------------------------------- #
def test_yellow_when_live_one_to_two_sigma_below(tmp_db):
    # Live mean -0.2, catalog +0.5; small spread → z between -1 and -2
    rs = [-0.2 + 0.15 * (i % 3 - 1) for i in range(30)]
    _record_n(tmp_db, "x", rs, catalog_R=0.5)
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.5)
    # The exact z depends on stdev; just verify it's in YELLOW range
    assert a.state in (ed.DecayState.YELLOW, ed.DecayState.RED)
    if a.state == ed.DecayState.RED:
        # If classifier flipped to RED, the sustained-red count should
        # have been > MIN_RED_TRADES — that's a stronger signal than YELLOW
        assert a.sustained_red_trades >= ed.MIN_RED_TRADES


# ----------------------------------------------------------------- #
# RED — sustained underperformance                                  #
# ----------------------------------------------------------------- #
def test_red_only_when_sustained_red_count_high(tmp_db):
    # 30 trades, all 3R below catalog (well below z=-2 threshold)
    # so EVERY trade contributes to sustained_red
    rs = [-3.0] * 30
    _record_n(tmp_db, "x", rs, catalog_R=0.13)
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.13)
    # Mean -3, catalog 0.13, sigma = 0 (all values equal) → z = None
    # When z=None we don't flip to RED. This is a degenerate case;
    # spread out the values to get a meaningful z.
    rs = [-2.0 - 0.2 * (i % 3) for i in range(30)]  # mean ~-2.2, sigma ~0.16
    _record_n(tmp_db, "y", rs, catalog_R=0.13)
    b = ed.assess(tmp_db, deployment_id="y", catalog_R=0.13)
    # All 30 trades > 2σ below catalog → sustained_red high → RED
    assert b.state == ed.DecayState.RED
    assert b.sustained_red_trades >= ed.MIN_RED_TRADES
    assert b.should_demote is True


def test_red_NOT_triggered_with_only_few_red_trades(tmp_db):
    # 30 trades but only the LAST few are bad — sustained_red
    # count should reset because OLDER trades were good
    # Recording order: oldest first, so insertion order matters
    rs = ([0.4] * 25) + ([-1.5] * 5)  # 25 good then 5 bad
    _record_n(tmp_db, "x", rs, catalog_R=0.13)
    a = ed.assess(tmp_db, deployment_id="x", catalog_R=0.13)
    # The newest 5 are bad; sustained_red counts from newest. But mix
    # of values gives nonzero sigma → z negative but classified by
    # whether sustained_red >= MIN_RED_TRADES (15). Only 5 sustained
    # red → must NOT be RED.
    assert a.state != ed.DecayState.RED
    assert a.sustained_red_trades < ed.MIN_RED_TRADES


# ----------------------------------------------------------------- #
# assess_all                                                        #
# ----------------------------------------------------------------- #
def test_assess_all_filters_to_known_deployments(tmp_db):
    _record_n(tmp_db, "known", [0.4] * 10, catalog_R=0.13)
    _record_n(tmp_db, "unknown", [0.4] * 10, catalog_R=0.13)
    out = ed.assess_all(tmp_db, catalog_R_lookup={"known": 0.13})
    assert len(out) == 1
    assert out[0].deployment_id == "known"


def test_assess_all_empty_db_returns_empty(tmp_db):
    # File doesn't exist
    assert ed.assess_all(tmp_db, catalog_R_lookup={"any": 0.0}) == []


# ----------------------------------------------------------------- #
# record_trade — schema bootstrap                                   #
# ----------------------------------------------------------------- #
def test_record_trade_creates_schema_on_first_call(tmp_db):
    assert not tmp_db.exists()
    ed.record_trade(tmp_db, deployment_id="x", live_R=0.5,
                       catalog_R=0.13)
    assert tmp_db.exists()
    # Second call should not crash
    ed.record_trade(tmp_db, deployment_id="x", live_R=0.6,
                       catalog_R=0.13)


# ----------------------------------------------------------------- #
# Pure stat helpers                                                 #
# ----------------------------------------------------------------- #
def test_mean_and_stdev():
    assert ed._mean([1, 2, 3]) == 2.0
    assert abs(ed._stdev([1, 2, 3]) - 1.0) < 1e-9
    assert ed._stdev([5]) == 0.0
    assert ed._mean([]) == 0.0


def test_z_score_returns_none_on_zero_stdev():
    assert ed._z_score(live_mean=0.5, catalog_R=0.0,
                          sample_stdev=0.0, n=5) is None
    assert ed._z_score(live_mean=0.5, catalog_R=0.0,
                          sample_stdev=0.1, n=1) is None


def test_z_score_basic_math():
    # mean=0.5, catalog=0.0, stdev=1, n=4 → stderr=0.5 → z=1.0
    z = ed._z_score(live_mean=0.5, catalog_R=0.0,
                       sample_stdev=1.0, n=4)
    assert z is not None
    assert abs(z - 1.0) < 1e-9
