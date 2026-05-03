# Grid Sweep Results

- Run config: comm=$3.0  slip=0.1×ATR  train_pct=0.6  long_only=True
- Tickers: `US100.cash, EU50.cash, USDJPY, GBPJPY, EURUSD, GBPUSD, AUDUSD, NZDUSD`
- Timeframes: `M15, H1, D1`
- 7 strategies (see scripts/sweep_grid.py)
- Total cells: 161
- Survivors (train AND test PF≥1, avg_R>0, n_test≥5): **9**

> Note: `money_per_unit_price=1.0` for ALL tickers, so $ amounts don't compare cross-symbol. PF and avg_R are scale-invariant and ARE comparable.

## ⭐ Edge candidates (positive in BOTH train and test)

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|---:|---:|---:|
| GBPJPY | D1 | ema_cross_12_26 | 21 | 1.35 | +0.148 | 8 | 3.69 | +0.952 |
| USDJPY | D1 | ema_cross_9_20 | 28 | 1.52 | +0.042 | 12 | 3.32 | +0.870 |
| GBPUSD | D1 | rsi_30_70 | 15 | 1.45 | +0.107 | 6 | 4.95 | +0.809 |
| USDJPY | D1 | ema_cross_12_26 | 20 | 1.34 | +0.213 | 10 | 3.03 | +0.739 |
| GBPJPY | D1 | ema_cross_9_20 | 28 | 1.27 | +0.151 | 12 | 1.98 | +0.520 |
| EURUSD | M15 | rsi_30_70 | 65 | 1.22 | +0.090 | 45 | 1.44 | +0.233 |
| USDJPY | D1 | donchian_20 | 38 | 1.09 | +0.126 | 25 | 1.20 | +0.093 |
| USDJPY | M15 | ema_cross_12_26 | 82 | 1.04 | +0.083 | 58 | 1.31 | +0.075 |
| EU50.cash | D1 | ema_cross_9_20 | 14 | 1.34 | +0.364 | 11 | 1.25 | +0.037 |

## Top 30 cells by test_R (out-of-sample avg R-multiple)

| ticker | tf | strategy | n_train | train_PF | train_R | n_test | test_PF | test_R | survivor |
|---|---|---|---:|---:|---:|---:|---:|---:|:---:|
| GBPUSD | D1 | ema_cross_12_26 | 22 | 0.69 | -0.180 | 8 | 10.00 | +1.335 |  |
| GBPJPY | D1 | ema_cross_12_26 | 21 | 1.35 | +0.148 | 8 | 3.69 | +0.952 | ⭐ |
| USDJPY | D1 | ema_cross_9_20 | 28 | 1.52 | +0.042 | 12 | 3.32 | +0.870 | ⭐ |
| GBPUSD | D1 | rsi_30_70 | 15 | 1.45 | +0.107 | 6 | 4.95 | +0.809 | ⭐ |
| EURUSD | D1 | ema_cross_9_20 | 19 | 0.86 | -0.032 | 12 | 2.77 | +0.746 |  |
| USDJPY | D1 | ema_cross_12_26 | 20 | 1.34 | +0.213 | 10 | 3.03 | +0.739 | ⭐ |
| GBPUSD | D1 | ema_cross_9_20 | 23 | 0.99 | -0.090 | 12 | 1.92 | +0.532 |  |
| GBPJPY | D1 | ema_cross_9_20 | 28 | 1.27 | +0.151 | 12 | 1.98 | +0.520 | ⭐ |
| EU50.cash | D1 | rsi_30_70 | 7 | 1.37 | -0.152 | 3 | 1.46 | +0.418 |  |
| US100.cash | M15 | ema_cross_9_20 | 105 | 0.72 | -0.142 | 53 | 1.48 | +0.274 |  |
| EURUSD | M15 | rsi_30_70 | 65 | 1.22 | +0.090 | 45 | 1.44 | +0.233 | ⭐ |
| US100.cash | M15 | donchian_20 | 132 | 0.82 | -0.140 | 90 | 1.27 | +0.226 |  |
| USDJPY | D1 | rsi_30_70 | 12 | 0.06 | -0.946 | 7 | 1.33 | +0.197 |  |
| GBPJPY | D1 | ema_pullback_20_50 | 59 | 0.83 | -0.167 | 31 | 1.45 | +0.195 |  |
| NZDUSD | D1 | rsi_30_70 | 18 | 0.52 | -0.489 | 7 | 1.17 | +0.195 |  |
| GBPUSD | M15 | bbands_20_2 | 136 | 0.64 | -0.215 | 80 | 1.34 | +0.157 |  |
| GBPJPY | D1 | bbands_20_2 | 28 | 1.11 | -0.076 | 21 | 1.59 | +0.153 |  |
| US100.cash | H1 | ema_cross_12_26 | 81 | 0.83 | -0.027 | 52 | 1.27 | +0.135 |  |
| GBPJPY | H1 | donchian_20 | 153 | 0.78 | -0.148 | 94 | 1.16 | +0.128 |  |
| US100.cash | M15 | ema_cross_12_26 | 83 | 0.64 | -0.236 | 45 | 1.11 | +0.126 |  |
| NZDUSD | D1 | ema_cross_12_26 | 18 | 0.44 | -0.637 | 13 | 1.15 | +0.125 |  |
| EURUSD | M15 | donchian_55 | 82 | 0.66 | -0.325 | 56 | 1.05 | +0.121 |  |
| EURUSD | M15 | bbands_20_2 | 150 | 0.71 | -0.158 | 98 | 1.41 | +0.115 |  |
| AUDUSD | M15 | bbands_20_2 | 145 | 0.73 | -0.184 | 105 | 1.30 | +0.108 |  |
| GBPUSD | H1 | rsi_30_70 | 67 | 0.99 | -0.029 | 41 | 1.22 | +0.107 |  |
| AUDUSD | H1 | donchian_55 | 82 | 0.87 | -0.194 | 71 | 1.16 | +0.106 |  |
| AUDUSD | M15 | rsi_30_70 | 51 | 0.95 | -0.057 | 39 | 1.04 | +0.106 |  |
| US100.cash | H1 | rsi_30_70 | 71 | 0.88 | -0.152 | 47 | 1.22 | +0.105 |  |
| USDJPY | D1 | donchian_20 | 38 | 1.09 | +0.126 | 25 | 1.20 | +0.093 | ⭐ |
| AUDUSD | H1 | donchian_20 | 158 | 0.74 | -0.235 | 106 | 1.05 | +0.085 |  |