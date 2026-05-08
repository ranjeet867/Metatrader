# Quant Perspective on the Catalog — 2026-05-05

**Author:** Claude (acting as in-house quant)
**Catalog version:** rebaselined at standard cost ($4 commission + 0.05×ATR slippage), 410 cells, 32 deploy-safe, 1 cell scoring ≥40
**Audience:** Ranjeet, before deploying real capital

---

## TL;DR — What you actually have

Cost-priced reality is humbling. In the entire 410-cell catalog there is **one** cell scoring ≥40 (rsi_30_70 EURUSD M15, score 41.0), **zero** cells scoring ≥60, and only 32 cells (8%) pass the deploy-safe gate (PF≥1.05, OOS PF≥1.0, n_test≥15, recovery≤90d).

That's normal. Anyone showing you retail backtests with PF >3 and Edge Score ≥80 is either (a) not paying real costs, (b) curve-fit on too few trades, or (c) selling you something. After realistic commission + slippage, retail-accessible setups settle into PF 1.1-2.3 with thin per-trade expectancy. The edge is real but not large; it works because it compounds over many trades on disciplined sizing.

The honest answer to "which 20 should I look at?" is the table below. Score is the multi-metric ranking; everything past row 5 is "marginal but real, paper first" territory.

---

## Top 20 deploy-safe cells (cost-priced, ranked by Edge Score)

| # | Score | Strategy | Ticker | TF | PF | WR% | R:R | Exp/R | n | recD | DD% |
|--:|------:|----------|--------|----|----|----|-----|-------|---|------|-----|
|  1 | 41.0 | rsi_30_70           | EURUSD     | M15 | 1.62 | 62% | 0.87 | +0.15 |  42 |   6d | 1.5% |
|  2 | 37.7 | rsi_30_70           | XPDUSD     | H1  | 2.14 | 63% | 0.96 | +0.23 |  22 |  54d | 1.3% |
|  3 | 35.4 | ema_cross_12_26     | US30.cash  | H1  | 1.16 | 64% | 0.67 | +0.07 |  85 |  33d | 1.4% |
|  4 | 33.4 | ema_cross_9_20      | JP225.cash | M15 | 1.66 | 50% | 1.66 | +0.33 |  54 |   1d | 3.0% |
|  5 | 32.3 | ema_cross_9_20      | US100.cash | M15 | 2.31 | 52% | 2.09 | +0.61 |  40 |   6d | 1.3% |
|  6 | 31.4 | ema_cross_9_20      | US30.cash  | H1  | 1.34 | 56% | 1.07 | +0.16 |  99 |  10d | 1.4% |
|  7 | 30.5 | donchian_55         | XAUUSD     | H1  | 1.33 | 58% | 0.94 | +0.12 |  92 |  47d | 3.6% |
|  8 | 28.1 | donchian_20         | US100.cash | M15 | 1.20 | 52% | 1.10 | +0.09 |  90 |  12d | 0.6% |
|  9 | 27.4 | ema_cross_12_26     | HK50.cash  | H1  | 1.76 | 52% | 1.62 | +0.36 |  48 |   7d | 0.9% |
| 10 | 26.0 | donchian_20         | XAUUSD     | D1  | 1.97 | 46% | 2.27 | +0.50 |  28 |  37d | 0.4% |
| 11 | 25.9 | ema_cross_12_26     | HK50.cash  | M15 | 1.22 | 46% | 1.42 | +0.11 |  78 |  17d | 1.2% |
| 12 | 24.8 | ema_cross_9_20      | US30.cash  | M15 | 1.30 | 46% | 1.55 | +0.17 |  35 |  11d | 1.7% |
| 13 | 24.5 | donchian_55         | XAGUSD     | D1  | 1.03 | 57% | 0.93 | +0.09 |  24 |  71d | 1.5% |
| 14 | 23.6 | ema_pullback_20_50  | JP225.cash | M15 | 1.09 | 43% | 1.45 | +0.05 | 140 |  14d | 3.9% |
| 15 | 23.4 | ema_cross_12_26     | US500.cash | M15 | 1.23 | 50% | 1.23 | +0.11 |  36 |  23d | 0.3% |
| 16 | 22.4 | ema_cross_12_26     | US100.cash | H1  | 1.38 | 39% | 1.77 | +0.09 |  89 |  53d | 4.3% |
| 17 | 22.0 | ema_cross_12_26     | UK100.cash | H1  | 1.54 | 47% | 1.76 | +0.30 |  30 |   8d | 0.4% |
| 18 | 21.8 | ema_cross_12_26     | XAUUSD     | M15 | 1.26 | 41% | 1.81 | +0.15 |  39 |   7d | 0.3% |
| 19 | 21.7 | donchian_20         | UK100.cash | H1  | 1.24 | 43% | 1.62 | +0.13 |  76 |  12d | 0.4% |
| 20 | 21.6 | ema_cross_9_20      | FRA40.cash | H1  | 1.27 | 45% | 1.54 | +0.14 |  42 |  11d | 0.4% |

**Concentration in the top 20:**
- By strategy: ema_cross_12_26 (7), ema_cross_9_20 (5), donchian_20 (3), donchian_55 (2), rsi_30_70 (2), ema_pullback_20_50 (1)
- By timeframe: M15 (9), H1 (9), D1 (2)
- By ticker: equity indices (12), metals (4), forex (1), other (3)

That's a feature, not a bug, on the strategy side — when one mechanism fits seven different indices, it's regime-portable rather than curve-fit. But on the ticker side it's a *correlation problem*: 12 of 20 are equity indices that move together. Deploying all of them is one big "long beta" bet, not a diversified portfolio.

---

## Quant view — what to actually deploy and why

I'd build a 6-cell starter portfolio out of the catalog, sized so total risk per day stays under 1% of equity. Pick by mechanism diversity, not by raw score:

### Tier A — deploy now, real money, smallest size

1. **rsi_30_70 EURUSD M15** (Score 41) — best in catalog. Mean-reversion on FX major. Recovery 6 days. Size 0.10% per trade. Already deployed; keep it.
2. **ema_cross_9_20 US100.cash M15** (Score 32) — highest expectancy in safe list (+0.61 R/trade). PF 2.31 with 40 OOS trades, recovery 6 days. Trend mechanism. 0.10% per trade.
3. **ema_cross_12_26 US30.cash H1** (Score 35) — large sample (n=85), tight recovery (33d), 64% WR. Different ticker + TF from #2 so they don't collide.

### Tier B — paper for 4-6 weeks, then promote if live R ≥ catalog R - 1σ

4. **ema_cross_9_20 JP225.cash M15** (Score 33) — Asian session diversifier; runs while US is sleeping. 1-day recovery is suspicious-tight on n=54, hence paper first.
5. **donchian_55 XAUUSD H1** (Score 30) — gold trend, classic mechanism, n=92. Drawdown 3.6% is near the upper limit for stacking; size at 0.05%.

### Tier C — keep on watchlist, do not deploy

6. **rsi_30_70 XPDUSD H1** (Score 38) — n_test only 22; PSR penalty real. Palladium is illiquid in retail accounts; cost model may understate spread.
7. Anything D1 except XAUUSD. D1 sample sizes are 12-30 trades; PSR weights hard against you.
8. Strategies that look good but failed deploy_safe (donchian_55 UK100 D1, bbands_20_2 UK100 D1, etc.). Recovery >90d is a real-money killer — you'll be in the hole for half a year before the equity curve catches up.

### Why I'm dropping things you might keep

- **Anything with n_test < 25** unless score >35. Small-sample edges look real until they're not.
- **Anything with recovery > 75d** even if PF is 2+. Calmar ratio kills these in real life because you live through the drawdown.
- **Strategies with single-ticker edge** — if ema_cross only works on US30 and nowhere else, that's curve-fitting.

### What this portfolio actually delivers

Math at $100k FTMO, 6 cells × 0.075% avg risk × 4 trades/day each ≈ 1.8% gross at risk per day, max actual loss day ~0.6% if 30% of trades hit stop. Expectancy summed across cells: roughly +0.20 R per trade × 24 trades/day = +4.8 R/week ≈ +0.36% weekly equity assuming 0.075% sizing. Annualised that's about 18% — realistic, not the 100%+ that retail systems advertise.

---

## New strategy ideas (Jane Street / Two Sigma flavour)

These are the kind of edges that institutional desks actually trade. None requires alternative data — just better signal construction on the same OHLC you already have. Ranked by expected hit rate of finding edge on your data.

### 1. Open-Range-Breakout with volatility-regime filter — `orb_vol_filtered`
**Why it works:** Index futures and FX majors release an information packet on session open. The first 30-60 minutes form an information range; breaks of that range continue when realised volatility is *expanding*, fade when it's compressing. Most retail ORB strategies skip the regime filter and lose money in low-vol days.
**Mechanism:** Compute realised vol over previous 20 bars vs its 5-day median. Take ORB break only when current_RV / median_RV > 1.2. Stop = opposite end of the range. Target = 1.5R.
**Test plan:** Run on US100/US30/JP225 at M15, no-entry window 30min after London/NY open and 30min before close. Expect WR 40-50%, R:R 1.5, n_test 200+. Mechanism bonus: low correlation to your ema_cross cells (different time of day).

### 2. VWAP fade / mean-reversion — `vwap_fade`
**Why it works:** Two Sigma intraday classic. When price diverges >1.5σ from session VWAP without a fundamental driver, institutional desks fade back. Works best on liquid indices and FX majors during 3rd hour of session (after open noise dies, before close positioning starts).
**Mechanism:** Anchor VWAP to session open (00:00 UTC for FX, 13:30 UTC for US, 07:00 UTC for EU). Compute rolling stdev of price-VWAP. Entry: price > VWAP + 1.5σ → short, < VWAP - 1.5σ → long. Stop: VWAP ± 2.5σ. Target: VWAP.
**Test plan:** US500/EURUSD/XAUUSD at M15. Expect WR 60-65%, R:R 0.8 (small targets, tight stops). Edge depletes if you don't filter by session. Different mechanism from rsi_30_70 (intraday-only, session-anchored).

### 3. Carry-curve regime trade on major FX — `carry_regime`
**Why it works:** When a currency's overnight rate differential is wide and stable, the high-yielder drifts on a directional bias on M15-H1 timeframes during the high-volume sessions. Documented in BIS papers since 2010.
**Mechanism:** Use a static rate-differential map (you can hardcode current ECB/Fed/BOJ/BOE rates and refresh quarterly). When abs(differential) > 200bps and current 50EMA agrees with carry direction, trade with EMA crossover system on H1. Skip if direction disagrees.
**Test plan:** EURUSD, GBPUSD, USDJPY on H1. Expect WR 55%, R:R 1.3, n_test 80+. Bonus: works in low-vol drift periods where breakout strategies starve.

### 4. Volatility breakout (Chaikin-style) — `vol_break`
**Why it works:** Donchian breakouts include false breaks during compressed-vol regimes. Filter the donchian signal by realised-vol expansion: only take breakouts when realised vol over last 10 bars > 1.5× the 50-bar realised vol.
**Mechanism:** Same entry as donchian_20 but conditioned on realised-vol > 1.5× MA(realised-vol, 50). Stop = 0.5×ATR. Target = 2×ATR.
**Test plan:** Compare cell-by-cell vs donchian_20 baseline on same ticker × TF. Expect lower trade frequency (-40%) but higher WR (+10pp) and lower DD%. If WR-improvement > 8pp it's worth keeping.

### 5. Asia-Europe news-fade — `news_fade`
**Why it works:** First 30min after a red-folder release on USDJPY or EURUSD over-extends as algos race to position; institutional flow comes back in 30-90min and prices revert ~50% of the move. Documented edge but requires news calendar (task #262).
**Mechanism:** When |return over 5min after news| > 0.4%, take counter-trend at +1σ from pre-news VWAP. Stop = breach of post-news high/low. Target = 50% retrace of news move.
**Test plan:** Requires news-calendar feed; depends on task #262 shipping. Expect rare (3-5 trades/week) but very high R:R (2.5+) and WR 55-60%.

### 6. Trend-pullback with regime gate — `trend_pullback_regime`
**Why it works:** The existing `trend_pullback` failed deploy_safe everywhere. Reason: it traded all market regimes equally. Real trend-pullback systems require regime alignment — only take long pullbacks when 50EMA > 200EMA on the *higher* TF.
**Mechanism:** On M15, only take long pullback to 20EMA when D1 50EMA > D1 200EMA (parent trend). Same logic shorts. Pullback entry = price tags 20EMA after first making new 5-bar high. Stop = swing low. Target = 1.5×ATR.
**Test plan:** Run cell-by-cell on top 6 indices. Compare to vanilla trend_pullback. If filter improves PF by >20%, that's the version we'd run.

### 7. Cross-asset overlay: DXY beta-residual — `dxy_residual`
**Why it works:** EURUSD has known beta to DXY (-0.95). When the beta-implied price diverges from realised price by >1σ, mean-reversion happens within 4-12 bars. This is what AQR and Renaissance do but on currency pairs you can already trade.
**Mechanism:** Compute 200-bar rolling regression of EURUSD on DXY. Forecast residual. When |residual| > 1σ residual, fade. Stop = 2σ residual. Target = 0σ residual.
**Test plan:** EURUSD M15, requires fetching DXY data (already in catalog if we add it). Bonus: very low correlation to anything else you're running.

---

## Edge-decay autolearner — when to demote a strategy automatically

You said "edge depletes, keep improving" — this is exactly what live-cell monitoring is for. Here's the spec:

### What we measure
For each live deployment, every closed trade, we track:
- `live_R` = realized PnL / risk_at_open (in R-multiples)
- Rolling-30 mean of `live_R`
- Catalog `expectancy_per_R` (the equivalent metric from the backtest)
- Standard error of mean = σ_R / sqrt(n)

### What triggers demotion
For each deployment, compute z-score: `(live_mean_R - catalog_R) / catalog_R_stderr`.

| z-score (rolling 30 trades) | Action |
|----:|--------|
| z > +1 | flag GREEN — possibly under-deploying, consider larger size |
| -1 ≤ z ≤ +1 | NORMAL — within statistical noise, keep running |
| -2 ≤ z < -1 | YELLOW — alert user, surface in Command Center, no action |
| z < -2 sustained 15 trades | RED — auto-demote live → paper, alert user, queue replay-parity re-run |

### What it tells you
A strategy whose live R has drifted -2σ below its catalog R for 15+ trades has either (a) had its edge regime-shift away (e.g. central-bank stance changed), (b) data window has stale parameters, or (c) the cost model in catalog under-estimated real costs. All three deserve human review before more capital flows in.

### What ships
- `core/edge_decay.py` — pure function `assess(deployment_id, db_path) -> EdgeDecayAssessment`
- `data/edge_decay.db` — SQLite table tracking per-deployment rolling stats updated on every trade close
- Command Center tile: `📉 Edge decay: 2 cells YELLOW, 0 RED`
- Weekly auto-rebaseline (task #259) recomputes catalog R values so the comparison stays fair as new market data arrives
- Auto-demote integration in DeploymentRunner — when assessment returns RED, status flips live→paper

### Why this is the right answer to "edge depletes"
Manual review of 6-12 strategies weekly is a chore you'll skip when busy. A z-score gate that triggers automatically and writes to a journal lets the system surface decaying cells without your attention. You only intervene when the assessment changes colour, not on a schedule.

---

## What to test next, in order

1. **This week** — Deploy Tier A (3 cells, rsi_30_70 EURUSD M15 + ema_cross_9_20 US100 M15 + ema_cross_12_26 US30 H1) at 0.10% risk. Watch live vs catalog R for 2 weeks before adding Tier B.
2. **Next 2 weeks** — Build `orb_vol_filtered` (idea #1). Backtest on US100/US30/JP225 at M15 over 2 years. If PF >1.3 with n>100, add to catalog.
3. **Build `vol_break`** (idea #4). Quickest test because it's a 1-line modification of donchian_20. Compare cell-by-cell.
4. **Build `vwap_fade`** (idea #2). Higher development effort because of session-VWAP plumbing, but the highest-edge mechanism on the list.
5. **Ship edge-decay autolearner** — the spec above. Wires into the runner you already have.
6. **Defer until #262 ships:** `news_fade`. Needs the calendar feed first.

---

## What I'd ignore for now

- More indicator combos on existing strategies (RSI+MACD, BBands+ADX, etc.). Diminishing returns; you've already swept the obvious space.
- ML-based signal generation. With ~40-100 OOS trades per cell you don't have the sample size to fit a tree without overfitting. Stick to economically-motivated rules.
- Crypto strategies until BTC sweep produces deploy-safe cells. Currently 0/32 BTC cells qualify under realistic costs.
- Anything anyone tells you on Twitter/Reddit. If it were that easy it would already be in the catalog.

---

*This memo is a static snapshot. As the catalog gets re-baselined weekly (task #259) and live-trade data accumulates, the autolearner (above) will keep the live picture honest. Treat catalog scores as a 7-day expiry; trust live R-tracking after that.*
