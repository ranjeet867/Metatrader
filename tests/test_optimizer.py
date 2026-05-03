"""
test_optimizer.py — pure-function tests for the autonomous portfolio
optimizer's scoring and ranking logic.

Doesn't run the actual backtest sweep (too slow for CI); that lives in
scripts/optimize_portfolio.py and is exercised manually. Here we pin
the math:

  • score_cell rewards the things a quant cares about and penalises
    drawdown + consec-loss streaks
  • rank_portfolio sorts highest-first and supports require_sustained
  • is_sustained correctly flags curves that touched the FTMO -10% floor
  • render_markdown emits a parseable table
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from core import optimizer
from core.backtest_stats import FullStats


def _stats(*, n=20, pf=1.5, r=0.4, dd=5.0, rr=1.5, max_streak=2,
            recov=10.0, win_rate=55.0, sum_realized=400.0,
            cagr=15.0) -> FullStats:
    return FullStats(
        n_trades=n, n_wins=int(n * win_rate / 100),
        n_losses=n - int(n * win_rate / 100),
        win_rate_pct=win_rate, sum_realized=sum_realized,
        profit_factor=pf, expectancy_dollars=sum_realized / max(1, n),
        avg_R=r, sharpe_R=0.5,
        avg_win_dollars=100.0, avg_loss_dollars=-50.0,
        risk_reward_ratio=rr,
        largest_win_dollars=300.0, largest_loss_dollars=-100.0,
        max_consec_wins=4, max_consec_losses=max_streak,
        max_dd_dollars=5_000.0, max_dd_pct=dd,
        max_dd_duration_days=10.0, recovery_duration_days=recov,
        span_days=180.0, cagr_pct=cagr,
        median_trade_bars=5, avg_trade_bars=8.0,
    )


# ---------------------------------------------------------------------------
# score_cell
# ---------------------------------------------------------------------------

def test_score_cell_rewards_high_pf_and_R():
    s_low = _stats(pf=1.0, r=0.0)
    s_high = _stats(pf=3.0, r=1.0)
    score_low = optimizer.score_cell(s_low, p_pass=0.5, sustained=True)
    score_high = optimizer.score_cell(s_high, p_pass=0.5, sustained=True)
    assert score_high > score_low + 2


def test_score_cell_penalises_drawdown():
    s_shallow = _stats(dd=2.0)
    s_deep = _stats(dd=20.0)
    a = optimizer.score_cell(s_shallow, p_pass=0.5, sustained=True)
    b = optimizer.score_cell(s_deep, p_pass=0.5, sustained=True)
    assert a > b
    assert a - b > 1.5   # 18% extra DD → at least 1.5 score difference


def test_score_cell_rewards_sustained():
    s = _stats()
    sustained = optimizer.score_cell(s, p_pass=0.5, sustained=True)
    breached = optimizer.score_cell(s, p_pass=0.5, sustained=False)
    assert sustained == breached + 5.0


def test_score_cell_rewards_p_pass():
    s = _stats()
    high = optimizer.score_cell(s, p_pass=0.9, sustained=True)
    low = optimizer.score_cell(s, p_pass=0.1, sustained=True)
    assert high > low + 7   # 80% diff × weight 10 = 8.0


def test_score_cell_penalises_long_loss_streaks():
    s_short = _stats(max_streak=2)
    s_long = _stats(max_streak=6)
    a = optimizer.score_cell(s_short, p_pass=0.5, sustained=True)
    b = optimizer.score_cell(s_long, p_pass=0.5, sustained=True)
    assert a == b + 10.0   # the streak-penalty constant


def test_score_cell_handles_inf_profit_factor():
    s = _stats(pf=float("inf"))
    score = optimizer.score_cell(s, p_pass=0.5, sustained=True)
    # pf is capped at 5 → score should be finite
    assert score == pytest.approx(score)
    assert score > 0


def test_score_cell_handles_inf_rr_ratio():
    s = _stats(rr=float("inf"))
    score = optimizer.score_cell(s, p_pass=0.5, sustained=True)
    assert score == pytest.approx(score)
    assert score > 0


# ---------------------------------------------------------------------------
# is_sustained
# ---------------------------------------------------------------------------

def test_is_sustained_when_curve_holds_above_floor():
    eq = pd.DataFrame([
        {"time": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "equity": 100_000.0},
        {"time": datetime(2026, 2, 1, tzinfo=timezone.utc),
         "equity": 95_000.0},   # -5% — above 10% floor
        {"time": datetime(2026, 3, 1, tzinfo=timezone.utc),
         "equity": 105_000.0},
    ])
    assert optimizer.is_sustained(eq, baseline=100_000)


def test_is_not_sustained_when_curve_breaks_floor():
    eq = pd.DataFrame([
        {"time": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "equity": 100_000.0},
        {"time": datetime(2026, 2, 1, tzinfo=timezone.utc),
         "equity": 89_000.0},   # -11% breach
    ])
    assert not optimizer.is_sustained(eq, baseline=100_000)


def test_is_sustained_empty_curve():
    assert optimizer.is_sustained(pd.DataFrame(), baseline=100_000)


def test_is_sustained_custom_floor():
    eq = pd.DataFrame([
        {"time": datetime(2026, 1, 1, tzinfo=timezone.utc),
         "equity": 100_000.0},
        {"time": datetime(2026, 2, 1, tzinfo=timezone.utc),
         "equity": 96_000.0},
    ])
    # Default 10% floor → still sustained
    assert optimizer.is_sustained(eq, baseline=100_000)
    # Stricter 3% floor → breached
    assert not optimizer.is_sustained(eq, baseline=100_000, floor_pct=3.0)


# ---------------------------------------------------------------------------
# rank_portfolio
# ---------------------------------------------------------------------------

def _cell(score, sustained=True, **kw) -> optimizer.CellScore:
    return optimizer.CellScore(
        strategy=kw.get("strategy", "x"),
        ticker=kw.get("ticker", "USDJPY"),
        tf=kw.get("tf", "D1"),
        rr_label=kw.get("rr_label", "1:2"),
        stop_atr_mult=1.5, target_atr_mult=3.0,
        risk_pct=0.5, n_test=10,
        test_pf=2.0, test_r=0.5, win_rate=55.0,
        max_dd_pct=5.0, recovery_days=10.0,
        max_consec_losses=2, rr_ratio=1.5,
        cagr_pct=20.0, p_pass_30d=0.7,
        score=score, sustained=sustained,
    )


def test_rank_returns_highest_score_first():
    cells = [_cell(10.0, strategy="A"), _cell(20.0, strategy="B"),
             _cell(15.0, strategy="C")]
    out = optimizer.rank_portfolio(cells, top_n=None)
    assert [c.strategy for c in out] == ["B", "C", "A"]


def test_rank_top_n_truncates():
    cells = [_cell(float(i)) for i in range(10)]
    out = optimizer.rank_portfolio(cells, top_n=3)
    assert len(out) == 3
    assert out[0].score == 9.0


def test_rank_require_sustained_drops_breached():
    cells = [
        _cell(20.0, strategy="A", sustained=False),
        _cell(15.0, strategy="B", sustained=True),
    ]
    out = optimizer.rank_portfolio(cells, require_sustained=True,
                                    top_n=None)
    assert [c.strategy for c in out] == ["B"]


def test_rank_no_filter_keeps_all():
    cells = [_cell(20.0, strategy="A", sustained=False),
             _cell(15.0, strategy="B", sustained=True)]
    out = optimizer.rank_portfolio(cells, top_n=None)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------

def test_render_markdown_has_header_and_rows():
    cells = [_cell(20.0), _cell(15.0)]
    md = optimizer.render_markdown(cells, run_meta="meta")
    # Header + table headings + 2 data rows
    assert "Portfolio optimization" in md
    assert "meta" in md
    assert "rank" in md
    # Two rows present
    body = [ln for ln in md.splitlines() if ln.startswith("| ") and "rank" not in ln and ":---" not in ln]
    # First two rows are the table header dividers; data rows are after
    data_rows = [ln for ln in md.splitlines() if ln.startswith("| 1 |")
                  or ln.startswith("| 2 |")]
    assert len(data_rows) == 2


def test_render_markdown_handles_inf_values():
    s = _cell(20.0)
    inf_cell = optimizer.CellScore(
        strategy=s.strategy, ticker=s.ticker, tf=s.tf,
        rr_label=s.rr_label,
        stop_atr_mult=s.stop_atr_mult, target_atr_mult=s.target_atr_mult,
        risk_pct=s.risk_pct, n_test=s.n_test,
        test_pf=float("inf"), test_r=s.test_r,
        win_rate=s.win_rate, max_dd_pct=s.max_dd_pct,
        recovery_days=s.recovery_days,
        max_consec_losses=s.max_consec_losses,
        rr_ratio=float("inf"),
        cagr_pct=s.cagr_pct, p_pass_30d=s.p_pass_30d,
        score=s.score, sustained=s.sustained,
    )
    md = optimizer.render_markdown([inf_cell])
    assert "inf" in md     # appears for both pf and rr columns


def test_render_markdown_handles_none_recovery_and_cagr():
    cells = [
        optimizer.CellScore(
            strategy="x", ticker="USDJPY", tf="D1", rr_label="1:2",
            stop_atr_mult=1.5, target_atr_mult=3.0, risk_pct=0.5,
            n_test=5, test_pf=2.0, test_r=0.3, win_rate=50.0,
            max_dd_pct=4.0, recovery_days=None,
            max_consec_losses=1, rr_ratio=1.5, cagr_pct=None,
            p_pass_30d=None, score=10.0, sustained=True,
        ),
    ]
    md = optimizer.render_markdown(cells)
    assert "—" in md   # fallback for None values
