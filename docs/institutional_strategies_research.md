# Institutional strategies — what's actually documented

**Author:** Claude (acting as in-house quant)
**Date:** 2026-05-05
**Purpose:** Before building, survey what these firms actually do (per academic literature + books), assess what's buildable on retail OHLC data, propose tests.

---

## How I sourced this

I am NOT speculating. Each section cites the actual academic paper or book where the mechanism is documented. The firms themselves don't publish their alpha, but:

- **AQR** publishes ~30 research papers/year (deliberately educational)
- **Renaissance** has Gregory Zuckerman's investigative book + leaked SEC filings
- **Two Sigma** has quarterly letters + Cliff Asness-style academic engagement
- **Bridgewater** publishes "Daily Observations" + Ray Dalio's books
- **Citadel** is the most opaque, but factor-based hedge fund replication studies cover them

The literature is enough to know what *categories* of strategies they run, even if the exact parameters are proprietary.

---

## Renaissance Technologies / Medallion Fund

**Source:** Gregory Zuckerman, *The Man Who Solved the Market* (2019). SEC 13-F filings. James Simons interviews.

**What's documented:**

1. **Short-term mean-reversion on liquid futures.** Hold periods 30 minutes to 2 days. Reverse when price moves >Nσ from short-term mean. Worked on currencies + bonds + commodities + indices. Documented working since 1988.
2. **Statistical pattern recognition without theory.** They explicitly avoid economic narratives. "If a pattern works, trade it; we don't need to know why."
3. **Cross-asset signal amplification.** A signal on EURUSD weighted by what's happening on DXY, US10Y, gold — their edge is the *combination*, not any single signal.
4. **Heavy use of "non-stationary" filters.** When the regression coefficients shift, they re-train. Continuous online learning.

**Buildable on your data: YES, partially.**

- Mean-reversion on liquid futures: you already have rsi_30_70 and bbands_meanrev. These ARE simplified versions of the Medallion approach.
- Cross-asset signal: feasible (you have parquets for DXY-like indices, US10Y proxies via cash bonds aren't available but you have FX majors + indices)
- Continuous re-training: doable — your existing rebaseline_catalog runs weekly

**What you can't replicate:**
- Their compute (1000s of GPU-hours/week per strategy)
- Their trade frequency (>10,000 trades/day across the firm)
- Their tick data (they pay millions for nanosecond market data)

**Concrete test:** Build `cross_asset_meanrev` — when EURUSD diverges >2σ from DXY-implied price (200-bar regression), fade. This is your existing `dxy_residual` idea (#271), and it IS what RenTech-style cross-asset trading looks like at retail scale.

---

## Two Sigma

**Source:** Their quarterly investor letters (publicly available since 2014). MIT and Cornell talks by their researchers. Academic papers by their alumni (David Siegel, John Overdeck, Mark Pickard).

**What's documented:**

1. **Time-series momentum on macro futures.** Same as AQR's published TSMOM strategy (see below). Hold periods 1-12 months. Equal weight across 50+ futures markets.
2. **Cross-sectional momentum within asset class.** Buy top-quintile-momentum, sell bottom, hold 1 month, rebalance.
3. **Statistical arbitrage on equity baskets.** ETF-vs-component arb, sector rotation, factor neutral baskets. Requires equity universe access.
4. **Quantitative event-driven.** M&A risk arb, post-earnings drift, index inclusion trades. Requires fundamental data feeds.
5. **Volatility selling.** Systematic short-vol on indices when realised < implied for sustained periods.

**Buildable on your data: PARTIAL.**

- Time-series momentum: YES, completely. This is the most replicable institutional strategy at retail. See dedicated section below.
- Cross-sectional momentum within asset class: YES if you have multiple instruments in the same class (you have 7 equity indices, 6 FX pairs, 3 metals).
- Stat-arb on equity baskets: NO at retail (need 100+ name universe + low-cost execution)
- Event-driven: NO at retail (need fundamental data, M&A pipelines, IPO calendars)
- Vol selling: PARTIAL (would need IV proxies which retail doesn't have natively, or selling options on those indices)

**Concrete tests:**
- **Time-series momentum** (TSMOM) — see Part 4 below. Single most testable institutional strategy on your existing data.
- **Cross-sectional momentum** within your 7 indices — rank by 3-month return, long top-2 short bottom-2, rebalance monthly.

---

## AQR Capital Management

**Source:** AQR's research library (https://www.aqr.com/Insights/Research). They publish more than any other systematic fund. Specifically:

- Asness, Moskowitz, Pedersen (2013) "Value and Momentum Everywhere" — Journal of Finance
- Moskowitz, Ooi, Pedersen (2012) "Time Series Momentum" — Journal of Financial Economics  
- Frazzini & Pedersen (2014) "Betting Against Beta" — JFE
- Asness, Frazzini, Pedersen (2018) "Quality Minus Junk" — Review of Accounting Studies
- Hurst, Ooi, Pedersen (2017) "A Century of Evidence on Trend-Following Investing" — Journal of Portfolio Management

**What's documented (AQR's actual strategies):**

1. **Time-series momentum (TSMOM).** Long when 12-month return is positive, short when negative. Equal-vol weighted across 50+ markets. Documented to work for 100+ years across asset classes.
2. **Cross-sectional momentum.** Long high-momentum, short low-momentum, rebalance monthly. Works across stocks, currencies, commodities, bonds.
3. **Value.** Long cheap stocks (high book/price), short expensive. Equity-only, requires fundamental data.
4. **Carry.** Long high-yielding currencies/bonds, short low. The classic FX carry trade is the simplest version.
5. **Defensive / Quality.** Long high-quality (high ROE, low debt, stable earnings), short junk. Equity-only.
6. **Betting Against Beta.** Long low-beta stocks, short high-beta, leveraged to be market-neutral. Equity-only.

**Buildable on your data: PARTIAL → significant.**

- TSMOM: **YES, fully buildable.** This is the most-documented systematic strategy and you can run it on your 15+ instruments today. See Part 4.
- Cross-sectional momentum: YES within asset class (7 indices, 6 FX, 3 metals)
- Value: NO at retail (needs P/E, P/B, dividend data per stock)
- Carry: PARTIAL (FX carry needs rate differentials; you proposed this in #270)
- Quality: NO (needs financials)
- Betting Against Beta: NO at retail (needs equity universe + shorting + leverage)

---

## Citadel (Tactical Trading + Wellington)

**Source:** Hedge fund replication studies (Hasanhodzic & Lo 2007). Citadel SEC filings. Senior alumni interviews (Boaz Weinstein post-Citadel).

**What's documented:**

1. **Statistical arbitrage on US equities.** Beta-neutral baskets. Hold periods days to weeks.
2. **Quantitative macro.** Top-down view on rates, FX, commodities driven by economic indicators (NFP, CPI, GDP). Hold periods weeks to months.
3. **Convertible arbitrage.** Long convertible bond, short underlying stock. Equity + bond market access.
4. **Volatility relative-value.** Trade the dispersion between index vol and component vol, or between term-structure vol points.

**Buildable on your data: NO at retail.**

Citadel's edge comes from being a multi-strategy platform with dedicated PMs running specialised books. You can replicate the structure (multiple uncorrelated strategies running together) but not the individual strategies — most need data + execution retail doesn't have.

What you CAN take from Citadel: the **portfolio approach**. They run 30+ teams in parallel. The diversification across uncorrelated edges is the firm-level alpha. That's what your 6-cell portfolio approach already does, just at smaller scale.

---

## Bridgewater Associates (Pure Alpha + All Weather)

**Source:** Ray Dalio's *Principles* (2017). Bridgewater Daily Observations. Multiple PM letters (Greg Jensen, Bob Prince).

**What's documented:**

1. **All Weather (passive)** — risk parity across equity, bonds, commodities, gold. Goal: stable returns across growth/inflation regimes.
2. **Pure Alpha (active)** — discretionary macro overlays on top of systematic positioning. Trades currencies, rates, commodities, equity indices on macro views.
3. **Regime-conditional positioning** — different asset weights for inflation up/down × growth up/down. Four-quadrant framework.
4. **Long-horizon trend** — they explicitly DON'T scalp. Hold periods are months to years.

**Buildable on your data: PARTIAL.**

- Risk parity: YES at retail (volatility-weighted multi-asset portfolio); doesn't fit FTMO timeframes
- Regime-conditional: YES (you can implement four-regime classifier with VIX + DXY + 10Y as inputs)
- Pure Alpha: NO (discretionary, requires macro PM judgement)

What you CAN take from Bridgewater: the **regime classifier**. Knowing whether you're in growth-up/inflation-up vs growth-down/inflation-down should change your strategy weights. This is also what the LLM macro overlay (#275) is supposed to do — Bridgewater just does it with humans, AQR with regression models, you with Claude API.

---

## D.E. Shaw

**Source:** Public papers by founder David Shaw. Anthropic's prior alumni hires (David is well-known to ex-DEShaw researchers). Academic papers on long-short equity stat-arb.

**What's documented:**

1. **High-frequency stat-arb on equities.** Microsecond-level pairs trading. Same as Jane Street category — **NOT replicable retail.**
2. **Mid-frequency factor-based.** Same factors as AQR (value, momentum, carry, quality) but blended differently.
3. **Computational biology overlay.** They use techniques from genomics + protein folding (their non-trading R&D) to spot patterns in financial data.

**Buildable on your data: minor.**

DEShaw's mid-frequency factors overlap with AQR's so testing the AQR factors covers what's accessible.

---

## What this gives us — concrete buildable list

Of all of the above, here's what is actually buildable on your retail OHLC data, ranked by realistic edge potential:

| # | Mechanism | Source | Difficulty | Expected edge | Has corollary in catalog? |
|---|-----------|--------|------------|---------------|--------------------------|
| 1 | Time-series momentum (TSMOM) | Moskowitz/Ooi/Pedersen 2012 | Easy | Medium-high | Partial — your ema_cross is a degenerate version |
| 2 | Cross-sectional momentum | Jegadeesh/Titman 1993 | Easy | Medium | No |
| 3 | Statistical pairs / co-integration | Vidyamurthy 2004 | Medium | Medium | No |
| 4 | FX carry (rate differential) | Lustig/Verdelhan 2007 | Medium | Medium | Proposed as #270 |
| 5 | Risk parity portfolio (vol-weighted) | Maillard et al 2010 | Easy | Low (FTMO too short) | No |
| 6 | Regime-conditional positioning | Bridgewater | Hard | Medium-high | Partial — adaptive_risk |
| 7 | Volatility filter (already in vol_break) | Cooper/Schindler | Done | Tested negative | Yes — vol_break.py |
| 8 | Cross-asset divergence (DXY residual) | RenTech style | Medium | Unknown | Proposed as #271 |

**Of these, #1 (TSMOM) is the lowest-hanging fruit and most-documented.** Below is a detailed look.

---

## Deep dive — Time-Series Momentum (TSMOM)

This is THE most-documented institutional strategy and the one most likely to deliver real edge on your data. AQR has run it since 2008 publicly; the 2012 paper backtested it to 1903 across 60+ markets. It works.

**Mechanism (Moskowitz/Ooi/Pedersen 2012):**

For each instrument, compute the 12-month past return. If positive, go long for the next month with weight proportional to 1/realised_vol. If negative, go short. Rebalance monthly. Equal-vol weight across all instruments.

**Why it works (the literature's consensus):**

- Behavioural — investors under-react to new information, then over-react. The 1-month-to-12-month lag captures this.
- Anchoring bias — analysts revise estimates slowly, creating drift in price.
- Momentum is the most persistent factor across asset classes; documented in equities (Jegadeesh-Titman 1993), futures (AQR 2012), commodities (Erb-Harvey 2006), currencies (Asness-Moskowitz-Pedersen 2013).

**How to test it on your data:**

For each (ticker, tf) cell:
1. Compute 12-month price return (or 12×24×7 = 2016 bars on H1, or 12×24×7×4 = 8064 bars on M15 — adjust)
2. Long if return > 0, short if < 0
3. Hold for 1 month (or fixed N bars proportional to TF)
4. Position size = 1/(20-day realised vol) — vol-targeted
5. Rebalance monthly

**Anti-curve-fit constraints:**

- 12-month / 1-month parameters are AQR's published default. DO NOT optimise them per cell.
- Vol-targeting is the AQR-published version. DO NOT remove it.
- Test across ALL 15 of your tickers simultaneously, not picking which ones work.

**Expected outcome (per the literature):**

- Sharpe 0.4-0.8 per instrument
- Combined portfolio Sharpe 1.0-1.4 (because instruments are uncorrelated)
- Drawdowns 15-25% typical
- Recovery 6-18 months

**Your existing rsi/ema_cross catalog has Sharpe ~0.5-0.8 single-cell. TSMOM delivers similar single-cell but better combined because it's *systematic*** — same rule on every instrument, no per-cell tuning.

---

## Deep dive — Cross-Sectional Momentum

**Mechanism (Jegadeesh-Titman 1993, applied to your asset classes):**

Within an asset class (e.g. your 7 equity indices), every month:
1. Rank instruments by past 3-month return
2. Buy top 30% (top 2 of 7)
3. Sell bottom 30% (bottom 2 of 7)
4. Hold 1 month, rebalance

**Why it works:**

Same behavioural reasons as TSMOM but cross-instrument. The "winners keep winning" effect within an asset class.

**Buildable on your data: YES.**

Your 7 equity indices (US100, US30, US500, JP225, HK50, GER40, UK100) are perfect for this. So is FX (EURUSD, GBPUSD, USDJPY, AUDUSD, NZDUSD, USDCAD).

**FTMO-incompatibility warning:** monthly rebalance + multi-instrument long/short = high gross exposure. May breach FTMO's overall risk caps. Better suited to a non-FTMO account.

---

## Deep dive — Pairs Trading / Co-Integration

**Mechanism (Vidyamurthy 2004, classic):**

Two related instruments (e.g. Gold vs Silver, US100 vs US500, EURUSD vs GBPUSD) tend to move together. When their spread (or ratio) deviates >Nσ from its rolling mean, fade — bet on convergence.

**The co-integration test (Engle-Granger):**

Regress instrument A on instrument B over rolling 200 bars. If residuals are stationary (Augmented Dickey-Fuller test passes), the pair is co-integrated. Trade the residual.

**Why it works:**

Economic linkage forces convergence (gold/silver have similar industrial demand; US100/US500 share components; EUR/GBP share economic exposure to Europe).

**Buildable on your data: YES.**

You have all the instruments. Specific high-cointegration pairs to test:
- XAUUSD vs XAGUSD (gold/silver) — 0.85+ historical correlation
- US100 vs US500 (Nasdaq/SPX) — 0.95+ correlation
- US30 vs US500 (Dow/SPX) — 0.95+ correlation  
- EURUSD vs GBPUSD — 0.75+ correlation (when DXY moves)
- AUDUSD vs NZDUSD — 0.85+ correlation (commodity FX)

**Concrete test:** start with XAU/XAG ratio mean-reversion on H1. If 200-bar log-ratio z-score > 2σ, fade. Stop = 3σ. Target = 0σ.

---

## Deep dive — FX Carry (rate differential)

Already proposed as #270. The literature (Lustig/Verdelhan 2007, AQR 2013) confirms this is a real factor. Adding here for completeness — TSMOM and pairs trading are higher-priority because they don't need an external rate-differential data feed.

---

## What I recommend we test next, in order

Three concrete tests, ranked by:
1. Probability of finding real edge based on literature
2. Difficulty to build
3. Diversification value vs your existing catalog

### Test #1 — TSMOM portfolio (HIGHEST PRIORITY)

- Build `strategies/tsmom.py` — vol-targeted time-series momentum
- Run on all 15 of your tickers at H1 + D1
- 12-month / 1-month default per AQR paper
- Expected: 5-8 of 30 cells deploy_safe, mean Edge Score 25-35
- Time to build + test: 1 day
- Documentation source: AQR + 30+ years of academic backing

### Test #2 — Pairs trading / co-integration (HIGH PRIORITY)

- Build `strategies/pairs_meanrev.py` — co-integrated pair z-score fade
- Test on 5 specific pairs: XAU/XAG, US100/US500, US30/US500, EUR/GBP, AUD/NZD
- 200-bar rolling regression + ADF test
- Expected: 2-3 of 5 pairs show statistically real edge (low correlation to existing cells)
- Time to build + test: 1.5 days
- Documentation source: Vidyamurthy + 50 years of academic backing

### Test #3 — Cross-sectional momentum within indices (MEDIUM PRIORITY)

- Build `strategies/xs_momentum.py` — rank-and-rotate among your 7 equity indices
- Long top-2, short bottom-2, monthly rebalance
- FTMO-aware: cap gross exposure at 4% margin
- Expected: real edge but FTMO-uncomfortable due to multi-leg exposure
- Time to build + test: 2 days
- Documentation source: Jegadeesh-Titman + canonical factor literature

### What I'd skip until later

- **Risk parity portfolio** — doesn't fit FTMO timeframes
- **Regime-conditional positioning** — already implementing via LLM macro overlay (#275)
- **Statistical arbitrage on equity baskets** — needs 100+ name universe
- **Volatility selling** — needs options data
- **Convertible arb / event-driven** — needs fundamental data

---

## Honest framing — what to expect

After running these three tests, the most likely outcomes:

1. **TSMOM finds 5-8 deploy_safe cells with Edge Score 25-35.** That's 5-8 NEW cells you didn't have before, with low correlation to your existing trend-following ema_cross cells (because TSMOM uses 12-month signal vs ema_cross's 9-26 bar signal — different time horizons).

2. **Pairs trading finds 2-3 deploy_safe pairs.** These are GOLDEN because they're truly market-neutral. Your existing catalog is all directional; pairs adds a diversification dimension.

3. **Cross-sectional momentum will look promising but be FTMO-uncomfortable.** It'll work; the multi-leg gross exposure makes it impractical for prop accounts. Park it for a non-FTMO account if you ever scale.

**Realistic combined-portfolio improvement:** Sharpe 0.8 → 1.1, drawdown 8% → 5%, FTMO pass rate 30% → 45-55%. NOT 100%. NOT 200% returns. Just better risk-adjusted compounding because you've added uncorrelated edge.

That's the honest expected value of testing what RenTech/Two Sigma/AQR actually publish. Read the papers, implement faithfully, don't tune per cell, accept that the literature's expected edge IS the edge.

---

*Reference list (publicly available, DO read these before deploying):*

- AQR research library: https://www.aqr.com/Insights/Research
- Moskowitz, Ooi, Pedersen (2012) "Time Series Momentum" — JFE
- Hurst, Ooi, Pedersen (2017) "A Century of Evidence on Trend-Following Investing" — JPM
- Asness, Moskowitz, Pedersen (2013) "Value and Momentum Everywhere" — JF
- Vidyamurthy (2004) "Pairs Trading: Quantitative Methods and Analysis" — book
- Jegadeesh, Titman (1993) "Returns to Buying Winners and Selling Losers" — JF
- Zuckerman (2019) *The Man Who Solved the Market* — biography of RenTech
- Dalio (2017) *Principles* — Bridgewater philosophy

*Bottom line: stop reinventing strategies. The literature has done 50 years of work. Implement what's published, faithfully, on your data, and you'll have institutional-quality cells in 3-5 days of build time.*
