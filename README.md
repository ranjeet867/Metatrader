# mt5_quant_trader_v2

**Production-grade MT5 quant trading research — rebuilt from scratch with rigor.**

## Why v2

v1 had bugs across multiple layers (executor balance not updating, divergent metric paths, vec_engine vs strategy_engine drift, EMERGENCY_STOP silently halting backtests). Every backtest result above the broken substrate was unreliable.

v2 is built test-first, with reconciliation invariants enforced at every layer.

## Core invariants

These MUST hold or every test fails:

1. **Idempotency**: Same input + same code = byte-identical output. Always.
2. **Reconciliation**: `sum(closed_trade.realized_pnl) == equity_curve[-1] - equity_curve[0]` within $0.01.
3. **Indicator parity**: Our pandas implementations match closed-form math AND MT5's output.
4. **Schema invariants**: Database can't accept duplicate trades, malformed candles, or out-of-order data.

## Project layout

```
core/
  data.py         — load + validate candles; deterministic
  indicators.py   — pure functions; tested against analytical math
  storage.py      — SQLite schema + idempotent persistence
  backtest.py     — equity-reconciled backtester
  strategy.py     — base class for strategies
  reconcile.py    — invariants checked at every backtest end

strategies/
  ema_cross.py    — the ONE locked strategy

tests/
  fixtures/synthetic.py    — generators with KNOWN expected outputs
  test_*                   — pytest, must all pass before any backtest is trusted

scripts/
  load_real_data.py   — fetch MT5 bridge → parquet
  run_backtest.py     — backtest entry point
```

## Quickstart

```bash
make setup        # create venv, install deps
make test         # all tests must pass
make load-data    # fetch US100.cash H1 from MT5 bridge → parquet
make backtest     # run EMA cross backtest on locked data
make dashboard    # launch the Streamlit control dashboard at :8501
```

## Control dashboard

A Streamlit dashboard at `dashboards/control.py` wraps the backtester for
interactive exploration: pick a ticker / timeframe / strategy, tune params,
inspect equity + drawdown + trade tape + reconciliation badge live. There's a
Sweep tab for multi-cell grids with a heatmap, and a Paper tab (UI stub —
real execution still needs MT5 Strategy Tester parity verification first).

Every run on the dashboard goes through `core.backtest.run_backtest` — the
dashboard never re-implements PnL math. See `dashboards/README.md` for details.

## Acceptance criteria for each phase

| Phase | Done when... |
|---|---|
| Indicators | EMA + Wilder's ATR match analytical formulas to 6 decimals |
| Backtest | Reconciliation invariant holds on 100+ random seeds |
| Strategy | Hand-crafted signal sequences produce exactly the expected trades |
| Real data | Loaded parquet passes schema + monotonicity + uniqueness checks |
| Integration | End-to-end on US100.cash H1 1y produces same numbers in two runs |

## Promotion path

Code in v2 earns promotion to "trustworthy" only after:

1. Pure-function unit tests pass with ≥ 95% line coverage
2. The integration reconciliation passes
3. (When available) MT5 Strategy Tester parity is within tolerance

Until that bar is met, results don't drive trading decisions.
