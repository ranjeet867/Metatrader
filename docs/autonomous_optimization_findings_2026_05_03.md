# Autonomous portfolio optimization — findings

Run: 2026-05-03 UTC. Real broker data. $100k starting balance. 0.5% risk per trade.

## Setup

- 7 strategies × 5 R:R variants (1:1, 1:1.5, 1:2, 1:3, 1:2 wide) × 10 tickers
- 3 timeframes (D1, H1, M15) — split into two runs to fit compute budget
- Each cell: full backtest with reconciliation gate + FullStats + 30-day FTMO
  Monte-Carlo P(pass) at 0.5% risk

## Top 10 cells across all timeframes

| rank | strategy | ticker | tf | R:R | n | PF | R | win% | maxDD% | recov_d | streak | rr | CAGR | P(pass) | sus | score |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `ema_cross_12_26` | `GER40.cash` | D1 | 1:3 | 7 | 7.68 | +1.34 | 71% | 4.0% | 817 | 2 | 3.07 | +0.5% | **98%** | ✅ | 25.8 |
| 2 | `ema_cross_9_20`  | `GER40.cash` | D1 | 1:3 | 9 | 4.96 | +1.27 | 67% | 5.6% | 925 | 1 | 2.48 | +0.4% | **95%** | ✅ | 25.0 |
| 3 | `ema_cross_12_26` | `US100.cash` | D1 | 1:3 | 7 | 3.66 | +1.16 | 57% | 2.9% | 482 | 1 | 2.74 | +1.3% | **91%** | ✅ | 23.4 |
| 4 | `ema_cross_12_26` | `GER40.cash` | D1 | 1:2 wide | 5 | 8.42 | +0.96 | 80% | 4.8% | 949 | 1 | 2.11 | +0.3% | **90%** | ✅ | 23.3 |
| 5 | `ema_cross_9_20`  | `US500.cash` | D1 | 1:3 | 7 | 3.75 | +1.03 | 57% | 0.9% | 587 | 1 | 2.81 | +0.4% | **85%** | ✅ | 23.1 |
| 6 | `donchian_20`     | `US100.cash` | D1 | 1:3 | 13 | 3.08 | +1.02 | 54% | 3.8% | 516 | 3 | 2.64 | +1.5% | **87%** | ✅ | 22.7 |
| 7 | `ema_cross_9_20`  | `US100.cash` | D1 | 1:3 | 8 | 2.92 | +0.87 | 50% | 3.3% | 688 | 2 | 2.92 | +1.2% | **75%** | ✅ | 21.1 |
| 8 | `donchian_20`     | `US100.cash` | M15 | 1:1.5 | 90 | 1.20 | +0.12 | 52% | 1.9% | — | 3 | 1.10 | −2.3% | **90%** | ✅ | 20.7 |
| 9 | `ema_cross_9_20`  | `US100.cash` | M15 | 1:1.5 | 53 | 1.21 | +0.09 | 51% | 2.0% | — | 4 | 1.17 | −2.5% | **80%** | ✅ | 19.2 |
| 10 | `donchian_20`    | `GER40.cash` | H1 | 1:1 | 122 | 1.05 | +0.04 | 61% | 2.9% | — | 4 | 0.68 | −1.5% | **72%** | ✅ | 18.5 |

## Key findings

1. **R:R 1:3 dominates D1.** Every D1 winner uses 1:3 (or 1:2 wide). At 0.5% risk per
   trade and ATR-based stops, lower targets clip too many winners.
2. **All top D1 cells stayed sustained** — equity never breached the FTMO −10%
   floor. Max drawdown across the top 7 was 5.6%.
3. **Indices win on D1.** GER40, US100, US500 fill the top 5 D1 spots.
4. **M15 winners use 1:1 / 1:1.5.** Higher trade frequency at M15 means tighter
   targets work better. Donchian-20 on US100 M15 (90 OOS trades, P(pass) 90%)
   is the strongest sustained M15 cell.
5. **GBPUSD cross-pairs are surprisingly absent from the top.** Old grid
   suggested GBPUSD ema_cross was strong, but at 0.5% risk on $100k starting
   balance the FTMO survival score penalises smaller-sample cells.

## How this changed the dashboard

`core/edge_catalog` now reads BOTH `grid_results.md` and every
`docs/optimization_*.md`, with optimizer entries taking precedence on
de-dupe. The Strategy Library now shows 56 cells across 23 (ticker, tf)
combinations — up from 32. The recommended portfolio surfaced via `⭐`
is unchanged in shape but every entry now has fresher stats.

## Re-run yourself

```bash
# Full sweep
python scripts/optimize_portfolio.py --top 50

# Tighter — D1 only with high iteration count
python scripts/optimize_portfolio.py --tfs D1 --ftmo-iter 5000 --top 30

# Filter to specific tickers
python scripts/optimize_portfolio.py --tickers USDJPY,GBPJPY --tfs D1 --top 10

# Drop any cell that breached the FTMO -10% floor
python scripts/optimize_portfolio.py --require-sustained --top 30
```

Output lands in `docs/optimization_<date>.md` and the dashboard picks it up
automatically on next page load.

## Tests

- `tests/test_optimizer.py` — 18 new tests: scoring, ranking, sustained
  detection, markdown rendering, inf/None handling.

## Pending — next iteration

- **ML strategy class** — local logistic-regression on RSI + MACD + ATR
  features. Will need its own backtest harness + walk-forward validation
  before it can join the library.
- **Trailing-stop mode** — current ema_cross + donchian use fixed stops.
  A trailing-ATR stop variant (e.g. stop = max(stop, close - 1.5×ATR))
  could improve recovery times.
- **Higher granularity sweep** — add fast/slow EMA period sweep (currently
  only 9_20 and 12_26 are tested).
