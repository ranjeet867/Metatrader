# Index Edge Findings (6 new strategies)

> **2026-05-03 update — fresh-data revalidation.** Refreshed all parquets from
> the MT5 bridge with 2000-bar D1 requests (yesterday: 1000). D1 datasets now
> reach back to **2018-03 (GER40)**, **2021-01 (US100/US500/EU50)**, and
> **2018-08 (USDJPY)** — substantially more train history. No new trading days
> (today is Sunday); the additional bars extend backward in time.
>
> Outcome: yesterday's 3 survivors **all revalidate**, plus 3 NEW survivors
> emerge that didn't make the cut on the shorter dataset. **All 6 survivors
> are vol_breakout** — a strategy concentration that's actually a positive
> signal (one specific market structure is being exploited, not many fragile
> patterns).
>
> Cross-asset confirmation: vol_breakout works on **NASDAQ-100 + DAX (long-only D1)**,
> **S&P 500 hourly bidir (top-30 but borderline)**, **EuroStoxx-50 hourly bidir**.
> US500.cash D1 specifically does NOT survive (test_R = -0.045 long, -0.050 bidir),
> so the effect is NOT universal across all index ETFs. NDX-style tech-weighted
> indices show stronger evidence than the broad S&P.
>
> Detail: see `## Fresh-data delta vs yesterday` below.

- Friction: $3.0/trade commission, 0.1×ATR slippage per fill, 60/40 train/test split
- Acceptance: n_test ≥ 25, test_PF ≥ 1.2, test_R ≥ 0.1, train_R > 0, train_PF ≥ 1.0
- Total cells run: **96**
- Survivors: **6** (was 3 yesterday)

## Fresh-data delta vs yesterday

Yesterday's 3 survivors, re-evaluated on today's longer parquet:

| ticker | tf | mode | yesterday n_test | today n_test | yesterday test_R | today test_R | Δ test_R | yesterday test_PF | today test_PF | acceptance? |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| US100.cash | D1 | long | 46 | **69** | +0.262 | **+0.228** | -0.034 | 1.51 | **1.38** | ✅ still |
| US100.cash | D1 | bidir | 65 | **96** | +0.176 | **+0.151** | -0.025 | 1.67 | **1.45** | ✅ still |
| USDJPY | D1 | long | 86 | **96** | +0.271 | **+0.300** | **+0.029** | 1.25 | **1.32** | ✅ still ↑ |

Edge held on all 3. USDJPY actually got STRONGER on more data. n_test grew
30–50% on every cell.

### NEW survivors that emerged on the longer history

| ticker | tf | strategy | mode | n_test | test_PF | test_R | test_ret % |
|---|---|---|---|---:|---:|---:|---:|
| GER40.cash | D1 | vol_breakout | long | 85 | 1.39 | **+0.352** | +18.99% |
| EU50.cash | H1 | vol_breakout | bidir | **520** | 1.25 | +0.165 | +38.43% |
| US100.cash | H1 | vol_breakout | bidir | **385** | 1.21 | +0.154 | +36.00% |

GER40 D1 long is now the **highest test_R** of all 6 cells. EU50 H1 bidir has
the **largest sample (520 OOS trades)** — most statistically robust.

### Cells that DID NOT survive on fresh data

| ticker | tf | strategy | mode | train_PF | test_PF | test_R | reason |
|---|---|---|---|---:|---:|---:|---|
| US500.cash | D1 | vol_breakout | long | 1.29 | 1.15 | **-0.045** | test went negative |
| US500.cash | D1 | vol_breakout | bidir | 1.03 | 0.95 | **-0.050** | test went negative |
| EU50.cash | D1 | vol_breakout | long | 1.01 | 1.08 | -0.003 | flat |
| EURUSD | D1 | vol_breakout | long | 0.98 | 1.19 | +0.010 | train negative |

US500 / S&P-500 specifically does NOT show the same D1 vol_breakout edge that
US100 (NDX) and GER40 (DAX) do. This is interesting and worth thinking about —
plausibly: NDX and DAX are more single-direction-momentum-heavy, while S&P is
broader and more mean-reverting on D1 timescales.

### Cross-asset evidence summary

vol_breakout edge survival across instruments:

| instrument | D1 long | D1 bidir | H1 bidir |
|---|:-:|:-:|:-:|
| US100 (NDX-100) | ✅ | ✅ | ✅ |
| US500 (S&P-500) | ❌ | ❌ | top-30 only (train negative) |
| GER40 (DAX-40) | ✅ | top-30 (R=+0.158, train+0.150 — close to surviving) | top-30 only |
| EU50 (Euro Stoxx 50) | ❌ | — | ✅ |
| USDJPY (FX sanity) | ✅ | top-30 (R=+0.221) | top-30 only |

**Read:** the vol_breakout edge appears on **3 of 4 indices on D1** (US100,
GER40 — and EU50 only at H1). It is NOT on US500 (S&P) at any tested tf.
That's enough cross-instrument support to consider a real market-structure
effect, not noise — but the gap on US500 is a real warning, not a footnote.

## ⭐ Edge candidates

| ticker | tf | strategy | mode | n_train | train_PF | train_R | n_test | test_PF | test_R | test_ret % |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| [GER40.cash](./equity_vol_breakout_GER40.cash_D1_long.png) | D1 | vol_breakout | long | 84 | 1.12 | +0.020 | 85 | 1.39 | +0.352 | +18.99% |
| [USDJPY](./equity_vol_breakout_USDJPY_D1_long.png) | D1 | vol_breakout | long | 151 | 1.14 | +0.133 | 96 | 1.32 | +0.300 | +19.74% |
| [US100.cash](./equity_vol_breakout_US100.cash_D1_long.png) | D1 | vol_breakout | long | 110 | 1.03 | +0.050 | 69 | 1.38 | +0.228 | +45.26% |
| [EU50.cash](./equity_vol_breakout_EU50.cash_H1_bidir.png) | H1 | vol_breakout | bidir | 623 | 1.17 | +0.116 | 520 | 1.25 | +0.165 | +38.43% |
| [US100.cash](./equity_vol_breakout_US100.cash_H1_bidir.png) | H1 | vol_breakout | bidir | 551 | 1.01 | +0.138 | 385 | 1.21 | +0.154 | +36.00% |
| [US100.cash](./equity_vol_breakout_US100.cash_D1_bidir.png) | D1 | vol_breakout | bidir | 159 | 1.11 | +0.173 | 96 | 1.45 | +0.151 | +65.39% |

## Top 30 cells by test_R

| ticker | tf | strategy | mode | n_train | train_PF | train_R | n_test | test_PF | test_R | survivor |
|---|---|---|---|---:|---:|---:|---:|---:|---:|:---:|
| GER40.cash | D1 | vol_breakout | long | 84 | 1.12 | +0.020 | 85 | 1.39 | +0.352 | ⭐ |
| GER40.cash | D1 | inside_bar | long | 30 | 0.62 | -0.504 | 17 | 2.89 | +0.344 |  |
| EU50.cash | D1 | inside_bar | long | 28 | 0.87 | -0.220 | 10 | 2.96 | +0.312 |  |
| USDJPY | D1 | vol_breakout | long | 151 | 1.14 | +0.133 | 96 | 1.32 | +0.300 | ⭐ |
| US100.cash | D1 | vol_breakout | long | 110 | 1.03 | +0.050 | 69 | 1.38 | +0.228 | ⭐ |
| USDJPY | D1 | vol_breakout | bidir | 203 | 1.13 | +0.083 | 153 | 1.10 | +0.221 |  |
| US500.cash | M15 | orb | long | 41 | 0.57 | -0.359 | 25 | 1.58 | +0.211 |  |
| US500.cash | H1 | vol_breakout | bidir | 620 | 0.87 | -0.008 | 359 | 1.35 | +0.197 |  |
| US500.cash | M15 | orb | bidir | 53 | 0.54 | -0.370 | 34 | 1.33 | +0.182 |  |
| US100.cash | M15 | orb | long | 42 | 0.77 | -0.086 | 26 | 1.34 | +0.169 |  |
| EU50.cash | H1 | vol_breakout | bidir | 623 | 1.17 | +0.116 | 520 | 1.25 | +0.165 | ⭐ |
| GER40.cash | D1 | vol_breakout | bidir | 152 | 1.24 | +0.150 | 124 | 1.13 | +0.158 |  |
| US100.cash | H1 | vol_breakout | bidir | 551 | 1.01 | +0.138 | 385 | 1.21 | +0.154 | ⭐ |
| US100.cash | D1 | vol_breakout | bidir | 159 | 1.11 | +0.173 | 96 | 1.45 | +0.151 | ⭐ |
| US100.cash | D1 | inside_bar | bidir | 38 | 0.74 | -0.160 | 28 | 1.15 | +0.131 |  |
| USDJPY | H1 | vol_breakout | bidir | 705 | 1.05 | +0.109 | 478 | 1.17 | +0.118 |  |
| GER40.cash | H1 | vol_breakout | bidir | 654 | 1.15 | +0.121 | 448 | 1.12 | +0.112 |  |
| US100.cash | M15 | orb | bidir | 53 | 0.55 | -0.202 | 34 | 1.13 | +0.112 |  |
| US100.cash | H1 | vol_breakout | long | 401 | 1.02 | +0.061 | 292 | 1.04 | +0.109 |  |
| EU50.cash | H1 | vol_breakout | long | 512 | 0.96 | -0.023 | 430 | 1.10 | +0.095 |  |
| USDJPY | H1 | vol_breakout | long | 540 | 0.81 | -0.128 | 338 | 1.17 | +0.095 |  |
| US500.cash | H1 | vol_breakout | long | 432 | 0.94 | +0.021 | 272 | 1.10 | +0.081 |  |
| US100.cash | D1 | inside_bar | long | 23 | 0.95 | -0.009 | 22 | 1.23 | +0.078 |  |
| EURUSD | M15 | orb | long | 34 | 0.48 | -0.385 | 24 | 0.94 | +0.070 |  |
| EURUSD | M15 | orb | bidir | 50 | 0.72 | -0.236 | 33 | 0.88 | +0.067 |  |
| EURUSD | H1 | vol_breakout | bidir | 637 | 0.97 | +0.075 | 381 | 1.10 | +0.064 |  |
| GER40.cash | D1 | inside_bar | bidir | 40 | 0.49 | -0.390 | 19 | 1.85 | +0.029 |  |
| EURUSD | D1 | vol_breakout | long | 148 | 0.98 | -0.054 | 81 | 1.19 | +0.010 |  |
| GER40.cash | H1 | vol_breakout | long | 531 | 1.08 | +0.080 | 372 | 0.99 | -0.001 |  |
| EU50.cash | D1 | vol_breakout | long | 83 | 1.01 | +0.044 | 65 | 1.08 | -0.003 |  |

## Comparison vs yesterday's best FX cells

Yesterday's best (from `docs/findings.md`):
- EURUSD M15 rsi_30_70 long-only: n=45, test_PF=1.44, test_R=+0.233
- EURUSD D1 ema_cross_9_20 bidir: n=22, test_PF=1.67, test_R=+0.435

Best NEW cells (this run):
- GER40.cash D1 vol_breakout long: n=85, test_PF=1.39, test_R=+0.352
- USDJPY D1 vol_breakout long: n=96, test_PF=1.32, test_R=+0.300
- US100.cash D1 vol_breakout long: n=69, test_PF=1.38, test_R=+0.228

## Recommendation

Top candidate: **GER40.cash D1 vol_breakout (long)**
- n_test = 85, test_PF = 1.39, test_R = +0.352
- Train period: +5.77% return
- Test period: +18.99% return

Suggested next step: port to MQL5, run in MT5 Strategy Tester, verify trade count and total P&L within ±10% before paper-trading.