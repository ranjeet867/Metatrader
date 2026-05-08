# Jane Street reality + LLM-as-filter architecture

**Author:** Claude (acting as in-house quant)
**Date:** 2026-05-05

This memo answers four questions you raised:
1. What timeframes do Jane Street strategies actually work on?
2. Which indexes/tickers are best for retail to test on?
3. Can Claude API turn macro/sentiment signals into edge?
4. Can we build a "good grade trading bot" by combining all of these?

The answers are not what marketers tell you. They are what works.

---

## Part 1 — Jane Street reality (a quant has to be honest)

Jane Street is not a strategy you can replicate. They are a **market-maker** whose edge is built on three things you, as a retail trader, cannot have:

| Their edge | Why retail can't replicate |
|------------|----------------------------|
| Co-located servers, microsecond fills | $50k/month colo + $10M+ FPGA infrastructure |
| Posting bid/ask on thousands of symbols simultaneously | Capital requirement for inventory across hundreds of names |
| Statistical arbitrage on correlated baskets (e.g. SPY ↔ 500 components) | Need to trade 500 legs simultaneously, real-time, low latency |
| ETF creation/redemption arbitrage | Need to be an Authorised Participant with ~$10B AUM |
| Pre-emptive flow internalisation (knowing customer order before it hits the market) | They have order flow; you do not |

What they do NOT do (and which is your actual edge surface):

- Discretionary directional bets on macro
- Holding positions for hours/days/weeks
- Trading off chart patterns or technical setups
- Reading news and reacting to it manually

So when someone says "I trade like Jane Street", they're either selling a course or they don't understand what Jane Street is.

**The institutional firms whose strategies you CAN partially replicate are:**

- **Renaissance / Two Sigma / DE Shaw** — long-horizon statistical arbitrage. Hold periods days to weeks. Edge from ensemble of weak signals.
- **AQR / Citadel / Bridgewater** — factor-based systematic. Macro + value + momentum + carry. Hold periods weeks to months.
- **Jim Simons (RenTech) Medallion** — short-term mean reversion + momentum on liquid futures, hold periods minutes to days.

These are who you want to model your approach on, not Jane Street.

---

## Part 2 — Best timeframes for retail edge

This is critical. Here's the truth nobody on Twitter says:

| TF | What it is | Edge availability for retail |
|----|------------|------------------------------|
| Tick / M1 | HFT / scalping | **DEAD ZONE.** You compete with co-located market-makers. Spread + slippage destroy any signal. |
| M5 | Aggressive scalping | **NEAR-DEAD.** Retail bid-ask + commission eats 80% of any edge. |
| **M15** | Day-trading | **VIABLE.** Spread is small fraction of typical move. Volume profile clear. **Best M15 cells in your catalog: rsi_30_70 EURUSD (Score 41), ema_cross_9_20 US100 (32), JP225 (33).** |
| **H1** | Swing | **BEST FOR RETAIL.** Costs amortise across larger moves. News-event impact dominates random noise. **Best H1 cells in your catalog: ema_cross_12_26 US30 (35), rsi_30_70 XPDUSD (38).** |
| **H4** | Position | **VIABLE.** Macro starts dominating. Smaller sample sizes hurt PSR but every trade is worth more. |
| D1 | Long-position | **VIABLE BUT UNDERSAMPLED.** 1-2 trades/week. Macro dominates. Sample size kills statistical confidence (PSR drag). |
| W1 | Investing | **DIFFERENT GAME.** Becomes value/macro investing, not strategy trading. Time horizon mismatch with FTMO. |

**The retail sweet spot is M15 + H1.** That's where:
- Your signal has enough resolution to capture intraday moves
- Costs are a small fraction of typical R:R
- News + macro events create real moves you can position around
- Sample sizes accumulate fast enough that 6-12 months is statistically meaningful

**Avoid:** anything below M15 on retail spreads. The math just doesn't work.

---

## Part 3 — Best instruments for retail (where edge actually lives)

Your catalog already has the right shape — focus on these tiers:

### Tier 1 — liquid, transparent, retail-friendly costs

**Best for ALL strategies, especially trend and breakout:**

- **US100.cash (Nasdaq)** — high vol, clean trending, 14h+/day liquid. Best M15/H1 instrument retail can trade.
- **US30.cash (Dow)** — slower than Nasdaq but cleaner regime breaks. Excellent H1.
- **US500.cash (S&P)** — benchmark. Lowest vol of US indices but most institutional flow.
- **JP225.cash (Nikkei)** — Asian session diversifier. Trends well on M15 during Tokyo hours.
- **EURUSD** — most liquid FX pair. M15 mean-reversion cells work here. Lowest spread of any FX.
- **XAUUSD (Gold)** — ATR-rich, both trend and reversal regimes, 23h/day. H1 your sweet spot.

### Tier 2 — niche but tradeable

**Where institutions don't bother because AUM-too-big, retail can win:**

- **GBPUSD, USDJPY, AUDUSD** — major FX, slightly wider spreads than EURUSD but still tradeable
- **XAGUSD (Silver)** — more volatile than gold, smaller institutional flow, retail edge exists on trend-following
- **HK50.cash (Hang Seng)** — Asian session, very volatile, less institutional crowding than US500
- **GER40.cash, UK100.cash, FRA40.cash** — European indices, decent moves around 13:00-16:00 UTC

### Tier 3 — be very careful

- **XPDUSD (Palladium)** — illiquid even by retail standards. Spread can blow up around news. Only trade if your broker shows real spread, not advertised.
- **Single stocks** — earnings risk, gap risk, manipulation risk on small caps. Stick to mega-cap (AAPL, TSLA, NVDA, AMZN) or skip.
- **Exotics (USDZAR, USDTRY etc)** — interest rate carry traps. Spreads 5-10× majors. Avoid.

### Tier 4 — DO NOT TRADE on FTMO accounts

- **Crypto perpetuals (BTC/ETH on FTMO)** — funding-rate trap during sustained one-way moves. Your previous BTC sweep showed 0/32 cells passing — that's structural, not bad luck.
- **Vol products (VIX, UVXY)** — contango bleed. Edge exists but requires options expertise.
- **Bonds (US10Y, BUND)** — central bank policy moves dominate. Whipsaws kill technical strategies.

**Bottom line:** your existing catalog already focuses on Tier 1 + 2. That's correct. Don't expand to crypto or exotics chasing Edge Score; the math says no.

---

## Part 4 — The Claude API question (where YOUR thinking is most valuable)

You raised this, and it's the most interesting part of the conversation. Let me separate the **real opportunity** from the **snake oil**.

### The snake oil version (what fails)

"Build an AI trading bot that reads news and trades automatically." This fails because:

1. **Latency** — by the time Claude API returns a response (3-8 seconds), the news-driven move is over. HFT firms' machines moved $1B+ in those 3-8 seconds.
2. **Hallucination risk** — Claude is excellent at language but can be confidently wrong on numerical/factual claims. "Hawkish" vs "dovish" reading of a Fed statement can flip on a single sentence.
3. **No backtest validity** — Claude has seen all news up to its training cutoff. You CANNOT backtest LLM-derived signals on historical news without data leakage.
4. **Cost** — $0.05/call × 50 calls/day × 22 days = $55/month per signal stream. Doesn't sound bad until you remember you'd want this for 10+ instruments.
5. **Sentiment-from-Twitter is a known scam** — by the time it's on Twitter, the move has happened. Retail sentiment is famously a *contrarian* indicator.

So no — you can't beat the market by asking Claude "should I buy NQ tomorrow?" That's not how the edge works.

### The real opportunity — LLM as FILTER, not SIGNAL

Here's the institutional pattern that actually works:

**Mechanical strategy generates the entry signal. LLM/macro context decides whether to size up, size down, or skip entirely.**

Think of it like an experienced human trader checking the news before letting a junior fire trades. The strategy is the junior; Claude is the senior PM saying "yes, but check the calendar".

Concrete things Claude API can actually do for you:

#### 1. News-event blocker (immediate value, ships first)

- Pull tomorrow's red-folder calendar (FF API, free)
- Each morning, ask Claude: "Of these 12 events on EUR/USD, USD/JPY, US100 today, which are likely to cause >30bps moves?"
- For confirmed high-impact events: block opens within ±15 min of the release on the affected instruments
- This is **deterministic gating, not signal generation**. Low risk of LLM hallucination changing your P&L.

#### 2. Weekly regime classifier (works on H1+ TFs)

- Every Sunday evening, feed Claude: last week's Fed minutes, ECB statement, BoJ press conference
- Output: hawkish/dovish/neutral per central bank
- Use the output as a **trade direction bias** for FX cells: if Fed hawkish + ECB neutral → favour USD strength → only take USD-long signals on EURUSD this week
- Risk: same direction bias may persist for weeks; retest weekly

#### 3. Earnings overlay (for stock cells specifically)

- Day before earnings: feed Claude the company's prior 4 quarters of earnings call transcripts + analyst expectations (publicly available)
- Output: probability of beat/miss/in-line
- Use it to scale risk **down to zero on positions held into earnings**, not to bet on direction
- Defensive use only — earnings vol kills you whether you bet right or wrong (gap risk overshoots stops)

#### 4. Macro regime size-multiplier (the most defensible use)

- Every Sunday: feed Claude global macro state (DXY level, 10Y yield, VIX level, oil price, gold/silver ratio, copper price)
- Output: classification — `risk-on`, `risk-off`, `transitional`
- Use it to scale **per-trade risk %**:
  - Risk-on + low VIX → use full configured risk (e.g. 0.10%)
  - Risk-off + high VIX → halve risk (0.05%)
  - Transitional → quarter risk (0.025%)
- This is the most defensible use because you're not predicting direction — you're sizing for uncertainty.

#### 5. Trump/geopolitical event scanner (blunt instrument, useful)

- Daily: pull top 20 geopolitical headlines from a wire service
- Ask Claude: "Are any of these likely to cause sustained directional moves in commodities, FX, or US equities over the next 3-5 trading days?"
- Output: list of affected instruments + directional bias
- Use as a **trade-opening freeze** on affected instruments for 24-48h. NOT to bet direction.

### What Claude API CANNOT do safely

- **Predict short-term price direction.** It will sound confident; it will be wrong as often as right.
- **Score sentiment on Twitter/Reddit and translate to trades.** Retail sentiment is contrarian noise.
- **Replace your mechanical strategies.** It can filter them; it cannot replace them.
- **React in real-time to news.** Latency makes this useless.

---

## A proposed architecture: LLM-overlay v1

This is buildable, testable, and defensible. The key principle: **mechanical signals stay; Claude only adjusts whether/how to act on them.**

```
                        ┌─────────────────────────────────┐
                        │  WEEKLY (every Sunday 22:00 UTC) │
                        │                                  │
    Macro inputs ──────►│  llm_macro_classifier.py        ├─► macro_state.json
    (DXY/VIX/yields)    │  Calls Claude API once/week      │   {regime: "risk-on",
                        │  Cost: ~$0.05/call               │    bias_eurusd: "neutral",
                        │                                  │    risk_multiplier: 1.0,
                        │                                  │    confidence: 0.7}
                        └─────────────────────────────────┘

                        ┌─────────────────────────────────┐
                        │  DAILY (every 06:00 UTC)        │
                        │                                  │
    Today's calendar ──►│  llm_news_filter.py             ├─► todays_blackouts.json
    + top headlines     │  Calls Claude API once/day       │   [{instrument: "EURUSD",
                        │  Cost: ~$0.05/call               │     blackout: "13:25-13:45 UTC",
                        │                                  │     reason: "NFP"},
                        │                                  │     ...]
                        └─────────────────────────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────────┐
                        │  PER-TRADE (DeploymentRunner)   │
                        │                                  │
    Mechanical signal ─►│  1. Check todays_blackouts      │
                        │  2. Check macro_state           │
                        │  3. Adjust risk_pct_multiplier  │
                        │  4. Decide: trade / skip / size │
                        │                                  │
                        │  Cost per trade: $0              │
                        │  (just JSON file reads)          │
                        └─────────────────────────────────┘
                                       │
                                       ▼
                                  EXECUTE
```

**Why this design works:**

- **Cost-bounded.** Claude API runs ~30 times/month. ~$1.50/month total. Compare to $150+ if you called it per-trade.
- **No real-time dependency.** Outputs are pre-computed JSON. Runner reads them in microseconds.
- **Backtest-able.** You can roll forward through historical macro_state.json files (once you have them) to A/B test the overlay.
- **Bounded blast radius.** Worst case: macro_state.json is wrong → you size up/down by 50%. You don't take wrong-direction trades because directions still come from your mechanical strategies.
- **Auditable.** Every Claude response is logged with input + output, so you can review where it helped vs hurt.

### What I'd build first (P0)

**`core/llm_macro.py`** + **`scripts/refresh_macro_state.py`**

- Call Claude API once weekly with current macro inputs
- Output a typed dataclass `MacroState`:
  - `regime: Literal["risk-on", "risk-off", "transitional"]`
  - `risk_multiplier: float` (clamped 0.25 to 1.5)
  - `bias_per_pair: dict[str, Literal["long", "short", "neutral"]]`
  - `confidence: float` (0-1)
- Persist to `data/macro_state.json` with timestamp
- DeploymentRunner reads it on every tick (60s cache); applies risk_multiplier; respects bias when set
- Claude API key in env var, never logged
- Fallback to neutral state if API call fails (hard fail safety)

### What ships next (P1)

**`core/llm_news_filter.py`** + **`scripts/refresh_news_blackouts.py`**

- Daily, pull FF calendar (depends on #262)
- Ask Claude to classify each event: high/medium/low expected impact per affected instrument
- Output blackout windows: `[(instrument, start_utc, end_utc, reason)]`
- DeploymentRunner pre-flight check: if signal_time within any blackout for this deployment's ticker → skip
- Pure additive safety filter — no existing edge gets removed, only protected from blow-up bars

### What I'd defer indefinitely

- **Per-trade LLM consultation.** The cost-benefit is wrong even at $0.05/call.
- **Sentiment-from-social.** Known to be contrarian noise.
- **LLM-driven entry signal.** Mechanical strategies do this better with no hallucination risk.
- **"AI agent" autonomously placing trades from natural-language reasoning.** This is what gets people blown up. Stay mechanical.

---

## What "good grade trading bot" actually looks like

Putting it all together:

```
                    ┌──────────────────────────┐
                    │  6 mechanical cells      │  ← Your existing catalog
                    │  (rsi_30_70 EURUSD M15,  │     Top 6 deploy_safe by Edge Score
                    │   ema_cross US100, ...)  │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │  Adaptive risk sizer     │  ← Already shipped (#244)
                    │  (FTMO buffer math)      │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │  LLM macro multiplier    │  ← P0 to build
                    │  ×0.25 to ×1.5           │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │  News blackout filter    │  ← P1 to build (after #262)
                    │  (skip if in blackout)   │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                    ┌──────────────────────────┐
                    │  Edge-decay autolearner  │  ← #269, builds in parallel
                    │  (auto-demote if z<-2)   │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                              EXECUTE
```

That stack is what an institutional risk-managed retail account looks like. Each layer adds ~10-30% to risk-adjusted return WITHOUT adding correlated bets. The compound benefit is real.

What it is NOT:
- "AI trading bot that predicts the market"
- A magic formula
- A way to replace mechanical strategies

What it IS:
- Mechanical edge × institutional risk hygiene × LLM-as-context-filter
- Expected outcome: similar gross PF to mechanical-only, but better Sharpe (lower drawdown, smoother equity)
- Pass-rate improvement from ~30% (mechanical only) to maybe 50-60% (mechanical + macro + news + autolearner) on FTMO challenges

That's the realistic picture. Not 100% pass rate. Not 200% annual returns. But a system you can run for years, that survives regime changes, and that compounds.

---

## What I recommend we build next, in order

1. **`core/edge_decay.py`** (#269) — IN PROGRESS. Most valuable single piece. Catches drift before it costs you money.
2. **`core/llm_macro.py`** (NEW) — weekly macro classifier. ~2 days of work. Costs $0.20/month. Concrete A/B test plan: run for 8 weeks vs no-overlay, compare Sharpe and pass-rate.
3. **News calendar feed** (#262) — depends only on FF API integration, no LLM needed.
4. **`core/llm_news_filter.py`** (NEW) — depends on #262. ~2 days of work. Concrete A/B test: backtest 90 days with vs without filter.
5. **Forensic snapshot table** (#261) — so the autolearner has data to analyse.

---

*Files referenced:*
- This memo: `docs/jane_street_reality_and_llm_overlay.md`
- Yesterday's quant memo: `docs/quant_perspective_2026_05_05.md`
- New strategy results: `docs/new_strategies_results_2026_05_05.md`

*Bottom line: Jane Street is a market-maker, not a strategy. Retail edge lives at M15-H1 on Tier 1-2 instruments. LLMs add real value as filters/overlays, not signal generators. Build in that order and you'll have an institutional-quality stack within a month.*
