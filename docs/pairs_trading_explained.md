# Pairs trading — what it is and why it works

**For:** before we build #279
**Why this doc exists:** read before building. Every concept, with examples, in plain English.

---

## The core idea (in one sentence)

Two related instruments that normally move together will, occasionally, drift apart for non-fundamental reasons. When they do, bet on convergence — long the cheap one, short the rich one. When the spread closes, take profit.

That's it. The whole strategy.

---

## A concrete example: gold and silver

Gold (XAUUSD) and silver (XAGUSD) are both precious metals. They have correlated demand drivers (jewelry, industrial use, store of value). Historically their **ratio** (XAU/XAG price) hovers around 70-90, occasionally drifting to 60 or 110 but always coming back.

When the ratio is at, say, 100 — gold is "rich" relative to silver. Statistically, the spread tends to revert. So you:

- **Short gold** (you think it'll fall, OR silver will catch up)
- **Long silver** (you think it'll rise, OR gold will fall)
- **Equal dollar amount on each leg** (so you're net-flat to overall metal direction)

When the ratio comes back to 85, your spread profit is positive **regardless of whether gold went down or silver went up** — what matters is the convergence, not the direction.

If gold goes up by 10% AND silver goes up by 15%, you lose on the gold short but make MORE on the silver long. Net positive.

That's the magic of pairs trading: **direction-neutral**.

---

## Why it works — the economic reason

In academic terms, gold and silver are **cointegrated**. Two series are cointegrated if there's a stable linear relationship between them that holds over the long run, even though both individual series wander randomly.

Why does the relationship hold? Because:
- Both metals are mined together (silver is often a byproduct of gold mining)
- Both serve as inflation hedges
- Both are stockpiled by central banks and ETFs
- Industrial demand for silver tracks economic activity, which also affects gold's safe-haven demand
- Substitution: jewelers swap between them when one becomes too expensive

When the ratio drifts away from its long-run equilibrium, **fundamental forces push it back**. Buyers see silver as "cheap" and bid it up; gold sellers take profit. The mechanism is real.

This is documented in finance literature for at least 50 years (Engle-Granger 1987, Vidyamurthy 2004 *Pairs Trading: Quantitative Methods and Analysis*).

---

## How we'd actually trade it (the mechanics)

### Step 1: Find a cointegrated pair

Run a statistical test (Engle-Granger) on the historical price series:

```
1. Regress price_A on price_B over the past 200 bars
   → produces hedge ratio β  (e.g. β = 0.85)
   → and residuals (the "spread")
2. Test if the residuals are stationary (ADF test)
   → if stationary → cointegrated ✓
   → if not → not cointegrated ✗ (don't trade)
```

The hedge ratio β is **how many units of B you short for every 1 unit of A you long**. For XAU/XAG, the regression typically gives β ≈ 70-90, meaning short 1 oz gold ↔ long ~80 oz silver.

### Step 2: Compute the z-score of the spread

Take the rolling residual (spread) and standardize it:

```
spread_t = price_A_t - β × price_B_t
z_t = (spread_t - rolling_mean) / rolling_stdev
```

z = 0 means "spread is at its average — fair value"
z = +2 means "spread is 2σ above average — A is rich vs B"
z = -2 means "spread is 2σ below average — A is cheap vs B"

### Step 3: Trade rules

- **z > +2** → SHORT A, LONG B (bet on convergence)
- **z < -2** → LONG A, SHORT B (bet on convergence)
- **z = 0** → close both legs, take profit
- **z > +3 (going wider)** → stop loss, exit at loss

That's the entire trading logic. Same parameters on every pair, no per-cell tuning.

### Step 4: Position sizing

Each leg gets HALF the per-trade risk. So if your `risk_pct` is 0.10%:
- 0.05% risk on the long leg
- 0.05% risk on the short leg
- Combined: 0.10% per spread trade (matches per-trade convention)

The two legs cancel partially — your true single-trade risk is *less* than 0.10% because they hedge each other.

---

## Why we'd test these specific 5 pairs

| Pair | Correlation | Economic linkage | FTMO-tradeable |
|------|------------:|------------------|----------------|
| **XAUUSD ↔ XAGUSD** | ~0.85 | Both precious metals, mined together | YES |
| **US100.cash ↔ US500.cash** | ~0.95 | Nasdaq is a subset of S&P 500 components | YES |
| **US30.cash ↔ US500.cash** | ~0.95 | Both US large-cap, overlap in mega-caps | YES |
| **EURUSD ↔ GBPUSD** | ~0.75 | Both DXY-driven, share Europe macro exposure | YES |
| **AUDUSD ↔ NZDUSD** | ~0.85 | Both commodity-FX, geographic neighbours | YES |

The high correlations are key — but raw correlation isn't enough. We need **cointegration**, which is correlation + mean-reverting spread. The Engle-Granger test confirms the second part.

These 5 are pre-screened by economic linkage (so we're not data-mining for spurious correlations) and by being on FTMO's tradeable list.

---

## What to expect — realistic numbers

Based on Vidyamurthy 2004 + post-2008 academic studies:

| Metric | Expected range | Reading |
|--------|---------------|---------|
| **Sharpe per pair** | 0.4-0.8 | Per-pair edge — single pair won't beat your top catalog cell |
| **Win rate** | 60-75% | High because most spreads do revert; the losses come from rare blow-outs |
| **R:R** | 0.5-0.8 | Targets are inside stops — many small wins, few large losses |
| **Trade frequency** | 1-3 per month per pair | Spreads only deviate >2σ occasionally |
| **Hold period** | 3-15 days | Convergence takes time |
| **Max drawdown** | 5-12% | Real risk: regime shifts that break cointegration |

**Combined-portfolio expected Sharpe: 1.0-1.4** if 3-4 pairs survive testing — they're uncorrelated to each other AND to your existing directional cells.

That's the value: not single-cell brilliance, but **uncorrelated additive Sharpe**.

---

## The risks (be honest about these)

### 1. Cointegration breaks

The biggest pairs-trading killer. Two examples:
- **March 2020 COVID:** EURUSD and GBPUSD decoupled for 2 weeks as UK had unique fiscal response. Spreads went to 4σ and stayed there. Pairs trades stopped out.
- **Brexit June 2016:** GBP fell 8% overnight; cointegration with EUR broke for months.

**Mitigation:** require recent ADF test (running cointegration check), tight stop at z = ±3σ, hard cap on max drawdown per pair.

### 2. Both legs going the wrong way

You shorted A, longed B. Then:
- A goes up (loses)
- B goes down (loses)
- Both legs lose → spread widened, you compound losses

This happens when the underlying economic linkage breaks. You stop out at z = 3σ to limit the bleed.

### 3. FTMO margin usage doubles

Pairs trading uses **two open positions**, not one. Margin requirement is the sum. On FTMO:
- Single 0.10% trade: ~$300 margin
- Pairs trade with 0.05% per leg: ~$300 margin TOTAL... still
- BUT: leverage and exposure show as 2× the notional

This is fine for FTMO's daily-loss caps (because risk is hedged), but **violates "one position per instrument" intuitive thinking**. You need to think in spread-units, not instrument-units.

### 4. Slippage on both legs

Spread = A − β×B. To enter, you need BOTH fills. If you fill A but B's price moves before B fills, you have asymmetric exposure. This is the #1 retail-execution problem.

**Mitigation:** Use limit orders only. Accept the rare missed-fill. Never market-order both legs simultaneously.

---

## What we'd actually build (the file structure)

If you say go, I'd write:

### `strategies/pairs_meanrev.py` — the strategy module

```python
@dataclass(frozen=True)
class PairsMeanRevParams:
    pair_b_ticker: str = "XAGUSD"   # the "B" leg (we trade A vs B)
    cointegration_window: int = 200  # ADF test lookback
    spread_window: int = 50          # z-score rolling window
    z_entry: float = 2.0
    z_exit: float = 0.0
    z_stop: float = 3.0
    require_adf_pass: bool = True    # skip signals if ADF says not coint.
```

The signals() method emits BOTH legs as separate Signal objects.

### `core/cointegration.py` — pure math

- `regress_hedge_ratio(a, b, window)` → returns β
- `compute_spread(a, b, beta)` → residual series
- `adf_test(series, alpha=0.05)` → True if stationary
- `z_score(series, window)` → standardised series

All pure functions, easy to unit-test.

### `scripts/sweep_pairs_meanrev.py` — the backtest runner

Test the 5 pre-screened pairs at H1 + D1, anti-curve-fit defaults (z_entry=2.0 across all pairs, no per-pair tuning), 60/40 train/test, full edge-score evaluation.

### `tests/test_pairs_meanrev.py`

Verify the math (regression, ADF, z-score) plus signal generation on a known cointegrated synthetic series.

---

## The realistic expected outcome

Of 5 pairs × 2 TFs = 10 cells tested:
- **2-4 cells** likely pass `deploy_safe` (per the literature's 30-40% survival rate on pre-screened economic pairs)
- **Top scorer probably 25-35** Edge Score (similar to mid-pack TSMOM cells)
- **Strongest candidate: XAU/XAG** (best documentation in literature, longest cointegration history)
- **2-3 cells** will look promising but fail recovery gate or have train/test asymmetry — same pattern as TSMOM

Combined with your 7 catalog cells (6 directional + 1 TSMOM), adding 2-3 pairs would take portfolio to 9-10 cells with **a fundamentally new mechanism** (market-neutral) that the existing catalog doesn't have.

---

## What I'd want from you before building

A go/no-go on these three points:

1. **Are you OK with two open positions per spread trade?** Each pair trade puts ONE long + ONE short on the broker simultaneously. That's normal pairs-trading; just confirming you understand the position-counting before we wire it up.

2. **Comfortable with low trade frequency?** Pairs trading produces 1-3 trades/month per pair. With 3 pairs running, that's 3-9 trades/month — plenty of statistical signal but not high-frequency excitement.

3. **Limit-orders only OK?** Pairs requires limit-order entry on both legs to prevent asymmetric fills. If your bridge currently uses market orders only, we'll need to add limit-order support to LiveExecutor.

If all three are yes, I build it. If any are no, we adjust the approach or skip pairs and go straight to the housekeeping batch.

---

*References:*
- Vidyamurthy (2004) *Pairs Trading: Quantitative Methods and Analysis* — the canonical book
- Engle, Granger (1987) "Co-integration and Error Correction" — Econometrica (the foundational paper)
- Gatev, Goetzmann, Rouwenhorst (2006) "Pairs Trading: Performance of a Relative-Value Arbitrage Rule" — Review of Financial Studies (large-scale empirical study, found Sharpe ~0.65 net of costs)

*Bottom line: pairs is well-documented, market-neutral, real. It's the right next add to your catalog because it's a genuinely different mechanism. But it has its own risks (cointegration breaks, double-fill execution) that don't exist in your current directional cells. Worth knowing before we commit to building.*
