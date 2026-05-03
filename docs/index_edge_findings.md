# Index Edge Findings (6 new strategies)

- Friction: $3.0/trade commission, 0.1×ATR slippage per fill, 60/40 train/test split
- Acceptance: n_test ≥ 25, test_PF ≥ 1.2, test_R ≥ 0.1, train_R > 0, train_PF ≥ 1.0
- Total cells run: **96**
- Survivors: **3**

## ⭐ Edge candidates

| ticker | tf | strategy | mode | n_train | train_PF | train_R | n_test | test_PF | test_R | test_ret % |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| [USDJPY](./equity_vol_breakout_USDJPY_D1_long.png) | D1 | vol_breakout | long | 141 | 1.23 | +0.154 | 86 | 1.25 | +0.271 | +14.32% |
| [US100.cash](./equity_vol_breakout_US100.cash_D1_long.png) | D1 | vol_breakout | long | 84 | 1.41 | +0.224 | 46 | 1.51 | +0.262 | +41.73% |
| [US100.cash](./equity_vol_breakout_US100.cash_D1_bidir.png) | D1 | vol_breakout | bidir | 122 | 1.19 | +0.205 | 65 | 1.67 | +0.176 | +66.66% |

## Top 30 cells by test_R

| ticker | tf | strategy | mode | n_train | train_PF | train_R | n_test | test_PF | test_R | survivor |
|---|---|---|---|---:|---:|---:|---:|---:|---:|:---:|
| EU50.cash | D1 | inside_bar | long | 28 | 0.87 | -0.220 | 10 | 2.96 | +0.312 |  |
| USDJPY | D1 | vol_breakout | long | 141 | 1.23 | +0.154 | 86 | 1.25 | +0.271 | ⭐ |
| US100.cash | D1 | vol_breakout | long | 84 | 1.41 | +0.224 | 46 | 1.51 | +0.262 | ⭐ |
| GER40.cash | D1 | inside_bar | long | 20 | 1.33 | +0.145 | 6 | 4.11 | +0.227 |  |
| US500.cash | M15 | orb | long | 41 | 0.57 | -0.359 | 25 | 1.58 | +0.211 |  |
| US500.cash | H1 | vol_breakout | bidir | 620 | 0.87 | -0.008 | 359 | 1.35 | +0.197 |  |
| US100.cash | H1 | vol_breakout | bidir | 599 | 1.00 | +0.108 | 432 | 1.24 | +0.193 |  |
| USDJPY | D1 | vol_breakout | bidir | 187 | 1.23 | +0.122 | 144 | 1.05 | +0.186 |  |
| US500.cash | M15 | orb | bidir | 53 | 0.54 | -0.370 | 34 | 1.33 | +0.182 |  |
| US100.cash | D1 | vol_breakout | bidir | 122 | 1.19 | +0.205 | 65 | 1.67 | +0.176 | ⭐ |
| US100.cash | H1 | vol_breakout | long | 445 | 1.05 | +0.076 | 315 | 1.09 | +0.143 |  |
| USDJPY | H1 | vol_breakout | bidir | 712 | 1.06 | +0.105 | 522 | 1.19 | +0.139 |  |
| EU50.cash | H1 | vol_breakout | bidir | 683 | 1.24 | +0.157 | 573 | 1.19 | +0.126 |  |
| GER40.cash | H1 | vol_breakout | bidir | 654 | 1.15 | +0.121 | 448 | 1.12 | +0.112 |  |
| USDJPY | H1 | vol_breakout | long | 558 | 0.82 | -0.124 | 364 | 1.17 | +0.097 |  |
| US100.cash | M15 | orb | bidir | 57 | 0.53 | -0.235 | 37 | 1.09 | +0.094 |  |
| EURUSD | M15 | orb | long | 36 | 0.54 | -0.371 | 27 | 0.97 | +0.087 |  |
| US500.cash | H1 | vol_breakout | long | 432 | 0.94 | +0.021 | 272 | 1.10 | +0.081 |  |
| EURUSD | M15 | orb | bidir | 54 | 0.71 | -0.254 | 36 | 0.91 | +0.080 |  |
| US100.cash | M15 | orb | long | 47 | 0.85 | -0.096 | 28 | 1.14 | +0.076 |  |
| GER40.cash | D1 | vol_breakout | long | 78 | 1.22 | +0.264 | 44 | 1.18 | +0.076 |  |
| EU50.cash | H1 | vol_breakout | long | 549 | 1.01 | +0.014 | 467 | 1.06 | +0.065 |  |
| EURUSD | H1 | vol_breakout | bidir | 648 | 1.01 | +0.122 | 428 | 1.08 | +0.046 |  |
| EURUSD | D1 | vol_breakout | long | 138 | 0.99 | -0.075 | 73 | 1.22 | +0.031 |  |
| GER40.cash | H1 | vol_breakout | long | 531 | 1.08 | +0.080 | 372 | 0.99 | -0.001 |  |
| EU50.cash | D1 | vol_breakout | long | 83 | 1.01 | +0.044 | 65 | 1.08 | -0.003 |  |
| US100.cash | D1 | overnight_drift | long | 586 | 0.57 | -0.024 | 399 | 0.53 | -0.027 |  |
| GER40.cash | D1 | overnight_drift | long | 586 | 0.54 | -0.026 | 399 | 0.51 | -0.029 |  |
| US500.cash | D1 | overnight_drift | long | 586 | 0.57 | -0.025 | 399 | 0.50 | -0.029 |  |
| USDJPY | D1 | overnight_drift | long | 1081 | 0.50 | -0.029 | 729 | 0.47 | -0.031 |  |

## Comparison vs yesterday's best FX cells

Yesterday's best (from `docs/findings.md`):
- EURUSD M15 rsi_30_70 long-only: n=45, test_PF=1.44, test_R=+0.233
- EURUSD D1 ema_cross_9_20 bidir: n=22, test_PF=1.67, test_R=+0.435

Best NEW cells (this run):
- USDJPY D1 vol_breakout long: n=86, test_PF=1.25, test_R=+0.271
- US100.cash D1 vol_breakout long: n=46, test_PF=1.51, test_R=+0.262
- US100.cash D1 vol_breakout bidir: n=65, test_PF=1.67, test_R=+0.176

## Recommendation

Top candidate: **USDJPY D1 vol_breakout (long)**
- n_test = 86, test_PF = 1.25, test_R = +0.271
- Train period: +14.87% return
- Test period: +14.32% return

Suggested next step: port to MQL5, run in MT5 Strategy Tester, verify trade count and total P&L within ±10% before paper-trading.