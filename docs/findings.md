# Cross-Ticker Strategy Survival Report

Run date: 2026-05-03
Branch: main (head: pre-grid-sweep)

## Test setup

- **Tickers (8)**: US100.cash, EU50.cash, USDJPY, GBPJPY, EURUSD, GBPUSD, AUDUSD, NZDUSD
- **Timeframes (3)**: M15, H1, D1
- **Strategies (7)**: ema_cross_9_20, ema_cross_12_26, ema_pullback_20_50, donchian_20, donchian_55, rsi_30_70, bbands_20_2
- **Total cells**: 161 (US100 D1 missing)
- **Friction modeled**:
  - $3 / trade commission (round-trip; FTMO retail estimate)
  - 0.1 × ATR(14) slippage per fill (entry AND exit, against the trade)
- **OOS partition**: first 60% in-sample, last 40% out-of-sample (by entry bar)
- **Per-ticker sizing**: lots × $/unit chosen so that 1×ATR ≈ $100–$520 per trade (~0.1–0.6% of $91.4k account)
- **Reconciliation gate**: enforced on every cell; halts run if violated
- **Min OOS trades for "robust" classification**: 20

## Survival criteria

A cell is a "robust edge candidate" iff ALL hold:
- reconciliation passes (mandatory)
- train_PF ≥ 1.0 AND test_PF ≥ 1.0
- train_avg_R > 0 AND test_avg_R > 0
- n_test ≥ 20

## Headline result

After friction + OOS partitioning, **5 of 161 cells survive** (3.1%) with statistically meaningful sample sizes:

### Bidirectional (long+short)
| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| EURUSD | D1 | ema_cross_9_20 | 31 | 1.02 | +0.217 | 22 | 1.67 | +0.435 |
| GBPUSD | D1 | ema_cross_9_20 | 34 | 1.09 | +0.099 | 21 | 1.40 | +0.246 |

### Long-only
| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| **EURUSD** | **M15** | **rsi_30_70** | 65 | 1.22 | +0.090 | **45** | **1.44** | **+0.233** |
| USDJPY | M15 | ema_cross_12_26 | 105 | 1.06 | +0.083 | 58 | 1.31 | +0.075 |
| USDJPY | D1 | donchian_20 | 38 | 1.09 | +0.126 | 25 | 1.20 | +0.093 |

## Highest-confidence candidates

1. **EURUSD M15 rsi_30_70 (long-only)** — most statistically robust.
   - 45 OOS trades, test_PF=1.44, test_avg_R=+0.233.
   - Mean-reversion buying RSI<30 dips on EURUSD, intraday horizon.
   - Trade count is high enough that a binomial confidence interval starts to mean something.

2. **EURUSD D1 ema_cross_9_20 (bidirectional)** — strongest test_PF.
   - 22 OOS trades, test_PF=1.67, test_avg_R=+0.435.
   - Classic trend-following on daily bars. Train_R also positive (+0.217), so the edge is consistent across the partition.
   - **However**: 22 trades is borderline; one outlier trade can move test_R noticeably.

3. **USDJPY M15 ema_cross_12_26 (long-only)** — highest sample size.
   - 58 OOS trades, test_PF=1.31, test_avg_R=+0.075.
   - Slim per-trade edge but very repeatable.

## What did NOT survive

- **Every M15+H1 strategy on indices** (US100.cash, EU50.cash) at our friction settings.
  US100 H1 ema_cross_12_26, which the user reported as "+15.9%/yr" pre-friction, drops to
  test_PF=1.25 / test_R=+0.170 — close to the survivor threshold but train_R is still negative,
  so it doesn't qualify as a consistent edge.
- **ema_pullback_20_50 anywhere with friction** — every tf/ticker combo is net-negative or wildly inconsistent train↔test.
- **Donchian breakouts on M15** — high trade frequency means commission destroys the edge.
- **All 7 strategies on AUDUSD/NZDUSD/GBPJPY in bidirectional mode at n_test ≥ 20**.

## Caveats (read these BEFORE paper-trading any of the survivors)

1. **Per-ticker sizing is approximate.** Real broker `tick_value`/`tick_size`
   differs slightly from our hardcoded `money_per_unit_price` (especially for
   FX pairs where the value depends on the prevailing rate). Verify with
   `make inspect-symbol` against your live broker before sizing real trades.

2. **Slippage of 0.1×ATR is a guess.** On D1 with ~150-pip ATR, that's 15 pips
   per fill — possibly conservative for liquid pairs at session open, possibly
   optimistic during news. Worth stress-testing at 0.2×ATR before paper.

3. **Only ~18 months of data** (Nov 2024 – May 2026). The OOS partition is
   ~7 months (last 40%). 22-58 OOS trades is a meaningful sample but not a
   full market regime cycle.

4. **MT5 Strategy Tester parity NOT yet verified.** Phase D of the original
   plan (port to MQL5, run in MT5 ST, compare trade-by-trade) is not done in
   this round because it requires GUI work. The Python backtester's
   reconciliation gate is internally consistent, but parity with MT5's
   actual fill mechanics remains an open question.

5. **No transaction costs other than $3/trade are modeled** — no swap/financing
   for D1 holds, no spread bias separate from slippage. D1 strategies hold
   overnight; if swap is materially negative on a pair, those P&Ls will erode.

6. **No regime / volatility / time-of-day filter.** The strategies as written
   take every signal. A volatility filter or session filter could potentially
   improve any of these, but that would be in-sample optimization unless
   designed before looking at OOS.

## What to do next

In rough priority:

1. **Verify EURUSD M15 rsi_30_70 in MT5 Strategy Tester** (Phase D). This is
   the candidate with the most data and a meaningful PF. If trade-count and
   total P&L match Python within ±10%, it's the one to paper.

2. **Verify EURUSD D1 ema_cross_9_20 in MT5 ST** as a second candidate. D1
   is friendlier for parity since fill timing is less ambiguous.

3. **Stress test at higher slippage (0.2×ATR) and higher commission ($5)**.
   If a candidate dies under realistic-pessimistic friction, it's not robust.

4. **Don't add more strategies yet.** The current 5 cover trend, pullback,
   breakout, RSI mean-rev, BB mean-rev — adding more before validating these
   risks p-hacking.

## Files written

- `docs/grid_results.md` — bidirectional, all cells (top 30 + survivors).
- `docs/grid_results_long_only.md` — long-only, all cells.
- `docs/grid_robust_bidir.md` — bidirectional, n_test ≥ 20.
- `docs/grid_robust_long_only.md` — long-only, n_test ≥ 20.
- `scripts/sweep_grid.py` — the grid runner; tweak `TICKER_CONFIG` to adjust per-ticker $/unit.

## Reconciliation status

All 161 cells reconciled within $0.01. The grid sweep also surfaced and fixed
a latent bug in the backtester: when a signal fired on the very last bar with
slippage > 0, the entry slippage leaked into the equity curve as floating PnL
(see `tests/test_backtest.py::TestSlippage::test_eod_close_when_signal_at_last_bar`).

77 / 77 unit + integration tests passing.
