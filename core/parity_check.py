"""
parity_check.py — single helper that runs BOTH run_backtest and replay_run
with IDENTICAL time-guard configuration, so the only thing that can diverge
is the engine code itself.

Why this file exists
--------------------
Replay accepts a TimeGuardCfg (which carries no_entry_minutes_before_close,
us_session_close_hhmm, flat_buffer_minutes, daily_close_flat_classes,
asset_class_overrides). run_backtest takes the SAME fields but as
individual kwargs. Several call sites built tg_cfg for replay but only
passed enforce_weekend_flat / enforce_daily_flat to backtest, leaving
no_entry_minutes_before_close at its default of 0. That single skew was
enough to cause the M15 + bidirectional divergence we saw on
ema_cross_9_20 × US100.cash × M15: at bar 2694 (bc_utc 20:00) replay's
no-entry window blocked the LONG entry, while backtest happily took it
because its own no_entry_minutes_before_close defaulted to 0.

Use this helper in:
  - dashboards/pages/B_🔬_Replay_Parity.py
  - dashboards/pages/A_📦_Portfolio_Composer.py (inline parity)
  - dashboards/pages/3_🟡_Paper.py (parity helper)
  - dashboards/pages/1_📊_Backtest.py (parity button)
  - any new diagnostic or test script
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.backtest import BacktestResult, run_backtest
from core.replay import ReplayResult, replay_run
from core.strategy import Strategy
from core.time_guards import TimeGuardCfg, time_guard_cfg_from_risk_config


@dataclass
class ParityRun:
    bt: BacktestResult
    rp: ReplayResult
    divergence_dollars: float

    @property
    def passes(self) -> bool:
        return self.divergence_dollars < 0.01 and self.bt.n_trades == self.rp.n_trades


def run_parity(
    candles: pd.DataFrame,
    strategy: Strategy,
    *,
    symbol: str,
    tf: str,
    starting_balance: float = 100_000.0,
    lots: float = 0.1,
    money_per_unit_price: float = 1.0,
    commission_per_trade: float = 0.0,
    slippage_per_fill_atr_frac: float = 0.0,
    risk_cfg=None,                # core.config.RiskConfig — preferred
    tg_cfg: TimeGuardCfg | None = None,   # or pre-built TimeGuardCfg
    symbol_info=None,
    risk_pct: float | None = None,
    sizing_uses_running_balance: bool = True,
) -> ParityRun:
    """Run backtest and replay against the same data and the same time-guard
    config, then return both results plus the absolute realized-PnL divergence.

    Either pass `risk_cfg` (core.config.RiskConfig) — we'll build the
    TimeGuardCfg from it — OR pass `tg_cfg` directly. If both are None,
    a fully-permissive TimeGuardCfg is used (all guards off).
    """
    if tg_cfg is None and risk_cfg is not None:
        tg_cfg = time_guard_cfg_from_risk_config(risk_cfg)
    if tg_cfg is None:
        # Default tg_cfg: all guards OFF (permissive). Daily-flat
        # explicitly disabled so parity tests don't accidentally
        # force-flat stocks/indices when the caller didn't ask for it.
        tg_cfg = TimeGuardCfg(
            weekend_flat_all=False,
            daily_close_flat_classes=("stock", "index"),
            us_session_close_hhmm="20:00",
            flat_buffer_minutes=5,
            no_entry_minutes_before_close=0,
            asset_class_overrides=None,
            enforce_daily_flat=False,
        )

    sigs = strategy.signals(candles)

    bt = run_backtest(
        candles, sigs,
        starting_balance=starting_balance,
        lots=lots,
        money_per_unit_price=money_per_unit_price,
        commission_per_trade=commission_per_trade,
        slippage_per_fill_atr_frac=slippage_per_fill_atr_frac,
        symbol=symbol,
        # ---- ALL time-guard fields, mirrored from tg_cfg ----
        enforce_weekend_flat=tg_cfg.weekend_flat_all,
        enforce_daily_flat=bool(tg_cfg.daily_close_flat_classes),
        asset_class_overrides=tg_cfg.asset_class_overrides,
        daily_close_flat_classes=tg_cfg.daily_close_flat_classes,
        us_session_close_hhmm=tg_cfg.us_session_close_hhmm,
        flat_buffer_minutes=tg_cfg.flat_buffer_minutes,
        no_entry_minutes_before_close=tg_cfg.no_entry_minutes_before_close,
        risk_pct=risk_pct,
        symbol_info=symbol_info,
        sizing_uses_running_balance=sizing_uses_running_balance,
    )
    rp = replay_run(
        candles, strategy,
        symbol=symbol, tf=tf,
        starting_balance=starting_balance,
        lots=lots,
        money_per_unit_price=money_per_unit_price,
        commission_per_trade=commission_per_trade,
        slippage_per_fill_atr_frac=slippage_per_fill_atr_frac,
        time_guard_cfg=tg_cfg,
        symbol_info=symbol_info,
        risk_pct=risk_pct,
        sizing_uses_running_balance=sizing_uses_running_balance,
    )
    div = abs(rp.sum_realized_pnl - bt.sum_realized_pnl)
    return ParityRun(bt=bt, rp=rp, divergence_dollars=div)
