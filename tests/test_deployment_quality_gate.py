"""
test_deployment_quality_gate.py — pin the deploy-quality math.

Every BLOCK rule has a regression test so a future loosen can't
silently let a bad-edge strategy slip into Live. WARN rules also
covered so we don't accidentally promote a small-sample cell.
"""
from __future__ import annotations

import pytest

from core import edge_catalog
from core.deployment_quality_gate import (
    QualityCriteria, QualityResult, evaluate_quality, load_criteria,
    save_criteria,
)


def _stat(**overrides) -> edge_catalog.EdgeStat:
    """Fresh EdgeStat with sensible 'green' defaults that pass every check."""
    base = dict(
        ticker="US100.cash", tf="D1",
        strategy="vol_breakout",
        n_train=50, train_pf=2.0, train_r=1.0,
        n_test=50, test_pf=2.0, test_r=1.0,
        rr_label="1:2", side="long",
        win_rate_pct=55.0, net_pnl_dollars=10_000,
        max_dd_pct=4.0, max_dd_dollars=4_000.0, max_dd_days=15,
        recovery_days=20.0, max_consec_losses=3,
        rr_ratio=2.0, avg_win_dollars=100.0, avg_loss_dollars=-50.0,
        cagr_pct=12.0, p_pass_30d=0.92, sustained=True, score=80.0,
    )
    base.update(overrides)
    return edge_catalog.EdgeStat(**base)


def test_green_zone_passes():
    """A clean strategy with all-green metrics returns OK."""
    s = _stat()
    r = evaluate_quality(s)
    assert r.is_ok, f"expected OK, got {r.verdict}: {r.block_reasons + r.warn_reasons}"
    # Sanity-check that the pass_notes captured at least the EV check
    assert any("EV per R" in p for p in r.pass_notes)


def test_block_negative_ev():
    """Negative expectancy must BLOCK — math says you lose money."""
    # 30% WR × 1:1 R:R => EV = 0.30*1 - 0.70 = -0.40R (red zone)
    s = _stat(win_rate_pct=30.0, rr_ratio=1.0, rr_label="1:1")
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("NO POSITIVE EDGE" in m or "RED ZONE" in m
                for m in r.block_reasons)


def test_block_low_win_rate():
    """Win rate below 35% (default floor) blocks."""
    s = _stat(win_rate_pct=25.0, rr_ratio=3.0)  # high R:R can't save it
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("WIN RATE TOO LOW" in m for m in r.block_reasons)


def test_block_excessive_drawdown():
    """Historical DD ≥ FTMO 10% floor blocks."""
    s = _stat(max_dd_pct=12.0)
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("BUSTS FTMO" in m for m in r.block_reasons)


def test_block_long_recovery():
    """Recovery > 200 days blocks (default)."""
    s = _stat(recovery_days=300.0)
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("RECOVERY TOO SLOW" in m for m in r.block_reasons)


def test_block_never_recovered():
    """recovery_days=None with significant DD blocks (still underwater)."""
    s = _stat(recovery_days=None, max_dd_pct=5.0)
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("NEVER RECOVERED" in m for m in r.block_reasons)


def test_no_block_when_recovery_none_but_tiny_dd():
    """If max_dd ≤ 1% and recovery is None, that's not a 'never recovered'
    issue — DD is too small to matter."""
    s = _stat(recovery_days=None, max_dd_pct=0.5)
    r = evaluate_quality(s)
    # No block from recovery — only blocks if other rules fail (none here)
    assert not any("NEVER RECOVERED" in m for m in r.block_reasons)


def test_block_long_losing_streak():
    """8 consecutive losses (default ceiling) blocks."""
    s = _stat(max_consec_losses=10)
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("STREAK" in m for m in r.block_reasons)


def test_block_small_sample():
    """OOS sample below 10 trades blocks (statistically unreliable)."""
    s = _stat(n_test=5)
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("SAMPLE TOO SMALL" in m for m in r.block_reasons)


def test_block_low_ftmo_pass_rate():
    """FTMO Monte-Carlo pass below 50% blocks."""
    s = _stat(p_pass_30d=0.30)
    r = evaluate_quality(s)
    assert r.is_blocked
    assert any("FTMO PASS RATE" in m for m in r.block_reasons)


def test_warn_small_sample_above_block_threshold():
    """OOS sample 15 (above 10 BLOCK floor, below 30 WARN) returns WARN."""
    s = _stat(n_test=15)
    r = evaluate_quality(s)
    assert r.verdict == "WARN"
    assert any("small OOS sample" in m for m in r.warn_reasons)


def test_warn_amber_trendo_zone():
    """Amber Trendo zone (break-even-ish EV) returns WARN."""
    # 50% WR × 1.05 R:R → EV ≈ 0.025R, which is amber
    s = _stat(win_rate_pct=50.0, rr_ratio=1.05, rr_label="1:1")
    r = evaluate_quality(s)
    assert r.verdict == "WARN"


def test_warn_consec_losses_threshold():
    """5-7 consec losses → WARN (above 5 warn, below 8 block)."""
    s = _stat(max_consec_losses=6)
    r = evaluate_quality(s)
    assert r.verdict == "WARN"
    assert any("max consec losses" in m for m in r.warn_reasons)


def test_warn_low_p_pass():
    """FTMO pass rate 60% (below 80% warn, above 50% block) returns WARN."""
    s = _stat(p_pass_30d=0.60)
    r = evaluate_quality(s)
    assert r.verdict == "WARN"


def test_streak_loss_busts_ftmo_blocks():
    """If suggested_risk × max_consec_losses ≥ FTMO floor, BLOCK."""
    s = _stat(max_consec_losses=5)
    # 2.5% × 5 = 12.5% > FTMO 10% floor
    r = evaluate_quality(s, suggested_risk_pct=2.5, ftmo_floor_pct=10.0)
    assert r.is_blocked
    assert any("STREAK × RISK" in m for m in r.block_reasons)


def test_streak_loss_warn_threshold():
    """Streak loss between 50-100% of floor warns."""
    s = _stat(max_consec_losses=4)
    # 1.5% × 4 = 6% — that's 60% of the 10% floor → WARN
    r = evaluate_quality(s, suggested_risk_pct=1.5, ftmo_floor_pct=10.0)
    assert any("close to FTMO floor" in m for m in r.warn_reasons)


def test_block_overrides_warn():
    """A WARN-level concern alongside a BLOCK still produces BLOCK."""
    # Multiple issues: tiny sample (BLOCK) + amber zone (WARN)
    s = _stat(n_test=3, win_rate_pct=50.0, rr_ratio=1.05)
    r = evaluate_quality(s)
    assert r.is_blocked


def test_lenient_criteria_let_more_through():
    """Lenient mode (paper) accepts what default (live) blocks."""
    s = _stat(n_test=8, max_consec_losses=10, recovery_days=350.0)
    strict = evaluate_quality(s, criteria=QualityCriteria.default())
    lenient = evaluate_quality(s, criteria=QualityCriteria.lenient())
    assert strict.is_blocked
    # Lenient may still BLOCK on other rules — but the 3 we relaxed
    # shouldn't drive it
    relaxed_reasons = " ".join(lenient.block_reasons)
    assert "SAMPLE TOO SMALL" not in relaxed_reasons
    assert "RECOVERY TOO SLOW" not in relaxed_reasons


def test_criteria_persistence(tmp_path, monkeypatch):
    """Per-account quality criteria save + load round-trip."""
    from core import account_manager
    monkeypatch.setattr(account_manager, "get_deployments_path",
                          lambda login: tmp_path / f"{login}/deployments.json")
    cfg_in = QualityCriteria(min_win_rate_pct=40.0, max_dd_pct=8.0,
                                max_recovery_days=120.0)
    save_criteria(login=12345, cfg=cfg_in)
    cfg_out = load_criteria(login=12345)
    assert cfg_out == cfg_in


def test_load_missing_returns_default(tmp_path, monkeypatch):
    from core import account_manager
    monkeypatch.setattr(account_manager, "get_deployments_path",
                          lambda login: tmp_path / f"{login}/deployments.json")
    cfg = load_criteria(login=99999)
    assert cfg == QualityCriteria.default()


def test_summary_line_format():
    """Summary line is single-line and starts with a status emoji."""
    s_ok = _stat()
    r_ok = evaluate_quality(s_ok)
    assert r_ok.summary_line().startswith("✅")

    s_bad = _stat(win_rate_pct=20.0, rr_ratio=1.0)
    r_bad = evaluate_quality(s_bad)
    assert r_bad.summary_line().startswith("⛔")


def test_needs_override_property():
    s_ok = _stat()
    s_warn = _stat(n_test=15)
    s_block = _stat(win_rate_pct=10.0)
    assert evaluate_quality(s_ok).needs_override is False
    assert evaluate_quality(s_warn).needs_override is True
    assert evaluate_quality(s_block).needs_override is True
