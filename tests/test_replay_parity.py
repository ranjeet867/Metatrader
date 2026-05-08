"""
test_replay_parity.py — INVARIANT-2: replay produces the same trades + PnL as
run_backtest for every (strategy, candles, params).

THIS IS THE GATE before paper or live trading. If any strategy fails parity,
we ROOT-CAUSE the bug — never add to a skip list.

What it checks:
  - For each of the 11 strategies on real US100.cash D1 candles AND on
    EURUSD H1 candles:
      * sum_realized_pnl(replay) ≈ sum_realized_pnl(backtest) within $0.01
      * n_trades(replay) == n_trades(backtest)
      * each trade's (direction, entry_bar_idx, close_reason, realized_pnl)
        matches its corresponding backtest trade

Markers:
  * @pytest.mark.slow — D1 parity is fast (~2k bars × 11 strats);
    H1 + M15 are slower (~8k bars). Use `make test-fast` to skip H1/M15 case
    when iterating.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.backtest import run_backtest
from core.data import load_parquet
from core.replay import replay_run
from strategies.bbands_meanrev import BBandsMeanRev, BBandsMeanRevParams
from strategies.donchian_breakout import DonchianBreakout, DonchianBreakoutParams
from strategies.ema_cross import EmaCross, EmaCrossParams
from strategies.ema_pullback import EmaPullback, EmaPullbackParams
from strategies.first30_meanrev import First30MeanRev, First30MeanRevParams
from strategies.ibs import Ibs, IbsParams
from strategies.inside_bar import InsideBar, InsideBarParams
from strategies.orb import Orb, OrbParams
from strategies.overnight_drift import OvernightDrift, OvernightDriftParams
from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
from strategies.vol_breakout import VolBreakout, VolBreakoutParams


REPO_ROOT = Path(__file__).resolve().parents[1]

# Module-level skip when broker parquets are absent. CI doesn't ship them
# (.gitignored personal data); locally `make refresh-data` populates them.
_REQUIRED_PARQUETS = [
    REPO_ROOT / "data" / "US100.cash_D1.parquet",
    REPO_ROOT / "data" / "EURUSD_H1.parquet",
]
_MISSING = [p.name for p in _REQUIRED_PARQUETS if not p.exists()]
pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=("requires broker parquets " + ", ".join(_MISSING)
            + " — run `make refresh-data` to populate"),
)


REPO = Path(__file__).resolve().parents[1]


# (strategy_class, params_factory, name_for_id)
ALL_STRATEGIES = [
    (BBandsMeanRev,    BBandsMeanRevParams,    "bbands_meanrev"),
    (DonchianBreakout, DonchianBreakoutParams, "donchian_breakout"),
    (EmaCross,         EmaCrossParams,         "ema_cross"),
    (EmaPullback,      EmaPullbackParams,      "ema_pullback"),
    (First30MeanRev,   First30MeanRevParams,   "first30_meanrev"),
    (Ibs,              IbsParams,              "ibs"),
    (InsideBar,        InsideBarParams,        "inside_bar"),
    (Orb,              OrbParams,              "orb"),
    (OvernightDrift,   OvernightDriftParams,   "overnight_drift"),
    (RsiMeanRev,       RsiMeanRevParams,       "rsi_meanrev"),
    (VolBreakout,      VolBreakoutParams,      "vol_breakout"),
]


def _build_strategy(strat_cls, params_cls):
    return strat_cls(params_cls())


def _check_parity(candles: pd.DataFrame, strat, *, money_per_unit: float,
                   lots: float, tolerance: float = 0.01,
                   commission: float = 0.0, slip: float = 0.0):
    """Run backtest + replay; assert numerical parity."""
    sigs = strat.signals(candles)
    bt = run_backtest(
        candles, sigs,
        starting_balance=100_000, lots=lots,
        money_per_unit_price=money_per_unit,
        commission_per_trade=commission,
        slippage_per_fill_atr_frac=slip,
    )
    rp = replay_run(
        candles, strat,
        symbol="X", tf="D1",
        starting_balance=100_000, lots=lots,
        money_per_unit_price=money_per_unit,
        commission_per_trade=commission,
        slippage_per_fill_atr_frac=slip,
    )
    # Both must individually reconcile
    assert bt.reconciles, f"backtest divergence={bt.reconcile_divergence}"
    assert rp.reconciles, f"replay divergence={rp.reconcile_divergence}"
    # Counts and totals must match
    assert rp.n_trades == bt.n_trades, (
        f"n_trades mismatch: backtest={bt.n_trades}, replay={rp.n_trades}"
    )
    assert abs(rp.sum_realized_pnl - bt.sum_realized_pnl) < tolerance, (
        f"PnL mismatch: backtest={bt.sum_realized_pnl:.4f} "
        f"replay={rp.sum_realized_pnl:.4f} "
        f"diff={rp.sum_realized_pnl - bt.sum_realized_pnl:.4f}"
    )
    # Per-trade alignment (entry_bar_idx + close_reason must match)
    for k, (bt_t, rp_t) in enumerate(zip(bt.trades, rp.trades)):
        assert bt_t.direction == rp_t.direction, (
            f"trade {k} direction: bt={bt_t.direction} rp={rp_t.direction}"
        )
        assert bt_t.entry_bar_idx == rp_t.entry_bar_idx, (
            f"trade {k} entry_bar: bt={bt_t.entry_bar_idx} rp={rp_t.entry_bar_idx}"
        )
        assert bt_t.close_reason == rp_t.close_reason, (
            f"trade {k} close_reason: bt={bt_t.close_reason} rp={rp_t.close_reason}"
        )
        assert abs(bt_t.realized_pnl - rp_t.realized_pnl) < tolerance, (
            f"trade {k} pnl: bt={bt_t.realized_pnl:.4f} rp={rp_t.realized_pnl:.4f}"
        )


@pytest.fixture(scope="module")
def us100_d1():
    return load_parquet(REPO / "data" / "US100.cash_D1.parquet")


@pytest.fixture(scope="module")
def eurusd_h1():
    return load_parquet(REPO / "data" / "EURUSD_H1.parquet")


@pytest.mark.parametrize("strat_cls,params_cls,name",
                          ALL_STRATEGIES,
                          ids=[s[2] for s in ALL_STRATEGIES])
def test_parity_us100_d1(us100_d1, strat_cls, params_cls, name):
    """All 11 strategies must replay-parity on US100.cash D1."""
    strat = _build_strategy(strat_cls, params_cls)
    _check_parity(us100_d1, strat, money_per_unit=1.0, lots=1.0)


@pytest.mark.slow
@pytest.mark.parametrize("strat_cls,params_cls,name",
                          ALL_STRATEGIES,
                          ids=[s[2] for s in ALL_STRATEGIES])
def test_parity_eurusd_h1(eurusd_h1, strat_cls, params_cls, name):
    """All 11 strategies must replay-parity on EURUSD H1.

    Marked slow because EURUSD H1 has ~8k bars, so the O(n) signals call per
    bar makes the test ~10× slower than the D1 case.
    """
    strat = _build_strategy(strat_cls, params_cls)
    _check_parity(eurusd_h1, strat, money_per_unit=100_000.0, lots=0.1)


def test_parity_with_slippage_us100_d1_vol_breakout(us100_d1):
    """Slippage path also must hold parity. Uses the survivor: vol_breakout
    on US100.cash D1, our flagship edge."""
    strat = VolBreakout(VolBreakoutParams(long_only=True))
    _check_parity(us100_d1, strat, money_per_unit=1.0, lots=1.0,
                  commission=3.0, slip=0.1)


# ---------------------------------------------------------------------------
# Regression: M15 + bidirectional + full time-guard cfg.
#
# Before the core/parity_check.run_parity fix, ema_cross × US100.cash × M15
# diverged by $2,068 because run_backtest was called with default
# no_entry_minutes_before_close=0 while replay_run received the full
# TimeGuardCfg with no_entry_minutes_before_close=30. At bar 2694
# (bc_utc=20:00, exactly the index session close) replay's no-entry window
# blocked a LONG entry while backtest's defaulted-off no-entry window let
# it through — every later trade in the divergence chain followed.
#
# This test pins both engines to identical guard cfg via run_parity().
# ---------------------------------------------------------------------------

_US100_M15_PATH = REPO_ROOT / "data" / "US100.cash_M15.parquet"


@pytest.mark.skipif(not _US100_M15_PATH.exists(),
                     reason="requires US100.cash_M15.parquet "
                            "(run `make refresh-data`)")
def test_parity_m15_with_no_entry_window():
    """ema_cross × US100.cash × M15 with no_entry_minutes_before_close=30
    and daily_close_flat=True — both engines must take identical trades."""
    from core.parity_check import run_parity
    from core.time_guards import TimeGuardCfg

    df = load_parquet(_US100_M15_PATH)
    strat = EmaCross(EmaCrossParams())

    tg = TimeGuardCfg(
        weekend_flat_all=True,
        daily_close_flat_classes=("stock", "index"),
        us_session_close_hhmm="20:00",
        flat_buffer_minutes=5,
        no_entry_minutes_before_close=30,
        asset_class_overrides=None,
    )
    pr = run_parity(
        df, strat, symbol="US100.cash", tf="M15",
        starting_balance=100_000.0, lots=1.0, money_per_unit_price=1.0,
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        tg_cfg=tg,
    )
    assert pr.bt.n_trades == pr.rp.n_trades, (
        f"n_trades skew: bt={pr.bt.n_trades} rp={pr.rp.n_trades}"
    )
    assert pr.divergence_dollars < 0.01, (
        f"PnL divergence ${pr.divergence_dollars:.4f}: "
        f"bt=${pr.bt.sum_realized_pnl:+.4f} rp=${pr.rp.sum_realized_pnl:+.4f}"
    )


# ---------------------------------------------------------------------------
# Regression: replay must accept risk%-sizing (lots=0.0) without crashing.
#
# Before this fix, the Backtest page's "Run Replay-Parity" button passed
# `lots=meta["lots"]` directly. When sizing_mode='risk %', `meta["lots"]` is
# hard-coded 0.0 and PaperExecutor.open() raised BadSignalGeometry("lots
# must be > 0; got 0.0"). The fix passes risk_pct + symbol_info instead,
# letting replay size each trade dynamically — matching the run_backtest
# behaviour. This test pins that contract.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _US100_M15_PATH.exists(),
                     reason="requires US100.cash_M15.parquet")
def test_parity_with_risk_pct_sizing_lots_zero():
    """When sizing_mode is risk%, replay must size dynamically off
    risk_pct + symbol_info, NOT crash on lots=0.0."""
    from core.parity_check import run_parity
    from core.symbol_info_loader import try_load
    from core.time_guards import TimeGuardCfg

    df = load_parquet(_US100_M15_PATH)
    strat = EmaCross(EmaCrossParams())
    sym_info = try_load("US100.cash")
    if sym_info is None:
        pytest.skip("symbol_info missing for US100.cash — "
                    "run `make refresh-symbol-info`")

    tg = TimeGuardCfg(
        weekend_flat_all=False,
        daily_close_flat_classes=("stock", "index"),
        us_session_close_hhmm="20:00",
        flat_buffer_minutes=5,
        no_entry_minutes_before_close=0,
        asset_class_overrides=None,
    )
    # The smoking gun: pass lots=0.0 + risk_pct=0.3 + symbol_info.
    # Pre-fix this would crash inside replay_run with BadSignalGeometry.
    pr = run_parity(
        df, strat, symbol="US100.cash", tf="M15",
        starting_balance=100_000.0,
        lots=0.0,                  # ← simulates risk%-sizing meta
        money_per_unit_price=1.0,
        commission_per_trade=4.0,
        slippage_per_fill_atr_frac=0.05,
        tg_cfg=tg,
        risk_pct=0.3,
        symbol_info=sym_info,
    )
    # Both engines must agree on n_trades + PnL within tolerance.
    assert pr.bt.n_trades == pr.rp.n_trades, (
        f"risk%-sizing parity skew: bt={pr.bt.n_trades} rp={pr.rp.n_trades}"
    )
    assert pr.divergence_dollars < 0.01, (
        f"risk%-sizing PnL divergence ${pr.divergence_dollars:.4f}"
    )
