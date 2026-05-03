# Grid Sweep Results

- Run config: comm=$3.0  slip=0.1×ATR  train_pct=0.6  long_only=False
- Tickers: `US100.cash, EU50.cash, USDJPY, GBPJPY, EURUSD, GBPUSD, AUDUSD, NZDUSD`
- Timeframes: `M15, H1, D1`
- 7 strategies (see scripts/sweep_grid.py)
- Total cells: 161
- Survivors (train AND test PF≥1, avg_R>0, n_test≥5): **5**

> Note: `money_per_unit_price=1.0` for ALL tickers, so $ amounts don't compare cross-symbol. PF and avg_R are scale-invariant and ARE comparable.

## ⭐ Edge candidates (positive in BOTH train and test)

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| EURUSD | D1 | ema_cross_9_20 | 31 | 1.02 | +0.217 | 22 | 1.67 | +0.435 |
| GBPUSD | D1 | rsi_30_70 | 26 | 1.36 | +0.120 | 18 | 1.86 | +0.419 |
| GBPUSD | D1 | ema_cross_9_20 | 34 | 1.09 | +0.099 | 21 | 1.40 | +0.246 |
| EU50.cash | D1 | ema_cross_9_20 | 23 | 1.06 | +0.160 | 15 | 1.29 | +0.120 |
| EU50.cash | D1 | ema_cross_12_26 | 21 | 1.11 | +0.140 | 15 | 1.22 | +0.059 |

## Top 30 cells by test_R (out-of-sample avg R-multiple)

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R | survivor |
|---|---|---|---:|---:|---:|---:|---:|---:|:---:|
| GBPUSD | D1 | ema_cross_12_26 | 37 | 0.63 | -0.163 | 16 | 3.36 | +0.850 |  |
| USDJPY | D1 | ema_cross_9_20 | 44 | 0.92 | -0.253 | 23 | 1.99 | +0.471 |  |
| EURUSD | D1 | ema_cross_9_20 | 31 | 1.02 | +0.217 | 22 | 1.67 | +0.435 | ⭐ |
| GBPUSD | D1 | rsi_30_70 | 26 | 1.36 | +0.120 | 18 | 1.86 | +0.419 | ⭐ |
| GBPJPY | D1 | ema_cross_12_26 | 28 | 0.74 | -0.175 | 15 | 1.51 | +0.377 |  |
| GBPJPY | D1 | rsi_30_70 | 37 | 0.93 | -0.003 | 11 | 1.98 | +0.346 |  |
| US100.cash | M15 | ema_cross_9_20 | 156 | 0.65 | -0.180 | 85 | 1.51 | +0.304 |  |
| EURUSD | D1 | ema_cross_12_26 | 33 | 0.88 | -0.049 | 16 | 1.31 | +0.259 |  |
| USDJPY | D1 | ema_cross_12_26 | 34 | 1.16 | -0.081 | 20 | 1.48 | +0.250 |  |
| GBPUSD | D1 | ema_cross_9_20 | 34 | 1.09 | +0.099 | 21 | 1.40 | +0.246 | ⭐ |
| NZDUSD | D1 | ema_cross_12_26 | 33 | 0.72 | -0.227 | 21 | 1.27 | +0.216 |  |
| US100.cash | H1 | ema_cross_12_26 | 146 | 0.81 | -0.091 | 94 | 1.25 | +0.170 |  |
| GBPJPY | H1 | ema_cross_9_20 | 187 | 0.62 | -0.280 | 89 | 1.22 | +0.166 |  |
| EU50.cash | D1 | bbands_20_2 | 42 | 0.80 | -0.101 | 30 | 1.15 | +0.134 |  |
| AUDUSD | M15 | bbands_20_2 | 270 | 0.72 | -0.164 | 154 | 1.34 | +0.130 |  |
| USDJPY | H1 | ema_cross_9_20 | 166 | 0.79 | -0.095 | 104 | 1.15 | +0.121 |  |
| EU50.cash | D1 | ema_cross_9_20 | 23 | 1.06 | +0.160 | 15 | 1.29 | +0.120 | ⭐ |
| NZDUSD | D1 | rsi_30_70 | 33 | 0.50 | -0.429 | 13 | 1.10 | +0.118 |  |
| US100.cash | H1 | ema_cross_9_20 | 175 | 0.70 | -0.164 | 113 | 1.12 | +0.110 |  |
| EURUSD | D1 | rsi_30_70 | 28 | 0.61 | -0.221 | 17 | 1.24 | +0.097 |  |
| USDJPY | D1 | donchian_20 | 63 | 0.89 | -0.138 | 40 | 1.08 | +0.081 |  |
| USDJPY | H1 | ema_cross_12_26 | 147 | 0.82 | -0.103 | 88 | 1.05 | +0.080 |  |
| AUDUSD | M15 | rsi_30_70 | 131 | 0.74 | -0.195 | 76 | 1.10 | +0.077 |  |
| USDJPY | M15 | rsi_30_70 | 124 | 0.61 | -0.217 | 79 | 0.93 | +0.066 |  |
| EU50.cash | D1 | ema_cross_12_26 | 21 | 1.11 | +0.140 | 15 | 1.22 | +0.059 | ⭐ |
| GBPJPY | D1 | ema_pullback_20_50 | 92 | 0.68 | -0.239 | 41 | 1.12 | +0.059 |  |
| USDJPY | H1 | donchian_20 | 304 | 0.77 | -0.150 | 177 | 1.10 | +0.055 |  |
| GBPUSD | D1 | bbands_20_2 | 59 | 0.91 | -0.150 | 44 | 1.03 | +0.030 |  |
| GBPUSD | H1 | rsi_30_70 | 139 | 0.66 | -0.236 | 82 | 1.03 | +0.022 |  |
| GBPJPY | D1 | bbands_20_2 | 56 | 0.87 | -0.187 | 44 | 1.13 | +0.018 |  |