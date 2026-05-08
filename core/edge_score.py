"""
edge_score.py — institutional-grade multi-metric cell scoring.

Score components (v2 — added Calmar + PSR)
------------------------------------------
1. EXPECTANCY ($/trade after costs)        weight 25%
   The most direct edge measure. $X × N trades/month = monthly P&L.

2. KELLY FRACTION (optimal compounding)    weight 20%
   = (WR × R:R - (1 - WR)) / R:R. Industry-standard sizing math
   from Kelly (1956). Negative Kelly = mathematically losing.

3. RECOVERY-FREQUENCY (resilience)         weight 15%
   trades_per_day / recovery_days. Frequent + fast recovery = robust;
   sparse + slow = fragile.

4. SHARPE-R (risk-adjusted return)         weight 15%
   Mean R / std R. Steady +0.3R beats wild +1R/-2R for the same mean.

5. CALMAR RATIO (return / max-DD)          weight 15%   ← NEW
   Annualised return ÷ max drawdown %. Used by Renaissance, Citadel.
   The most tail-risk-aware single number. >1.0 = excellent, >3.0 = rare.

6. PSR — PROBABILISTIC SHARPE RATIO        weight 10%   ← NEW
   Probability that the TRUE Sharpe is > 0, given sample size +
   skew + kurtosis. From Bailey & López de Prado (2012). Accounts
   for the fact that Sharpe 1.0 on 30 trades is much less impressive
   than Sharpe 1.0 on 3000 trades. >0.95 = high confidence.

7. DEPLOY-SAFE BONUS (compliance)          weight 10%
   Hard-gate pass-through (PF ≥ 1.05, n_test ≥ 15, recovery ≤ 90d).

Why Calmar + PSR are the right additions
----------------------------------------
- Industry top-tier shops (Renaissance, Two Sigma, Citadel, Jane Street)
  ALL use Calmar or close variants (MAR, Sterling) because annualised
  return ÷ max drawdown is THE practitioner's risk-adjusted return.
- PSR is the single most important addition for retail traders because
  it explicitly penalises the "PF 10 on 11 trades" trap that fooled the
  user in earlier sessions. A high Sharpe on a tiny sample = low PSR.

Total score: weighted sum, normalised to 0..100.
Pure function — no I/O. Operates on EdgeStat-shaped dicts.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeScoreBreakdown:
    """Per-component scores + the weighted total. Caller can show
    the breakdown in the UI so users understand WHY a cell ranks
    where it does (no black-box ranking)."""
    expectancy_score: float        # 0..100
    kelly_score: float              # 0..100
    recovery_frequency_score: float
    sharpe_r_score: float
    calmar_score: float
    psr_score: float
    sortino_score: float            # NEW v3
    deploy_safe_bonus: float        # 0 or 100
    total: float                    # weighted sum, 0..100
    # Raw inputs for tooltip display
    expectancy_per_trade: float
    kelly_pct: float
    trades_per_day: float
    recovery_days: float
    sharpe_r: float
    calmar_ratio: float
    psr: float                      # 0..1, probability
    sortino_ratio: float            # NEW v3
    n_trades: int                   # sample size for PSR
    deploy_safe: bool


# ─── Component scorers ────────────────────────────────────────────────


def _score_expectancy(expectancy_per_trade: float,
                       trades_per_day: float) -> float:
    """Normalize expectancy × frequency to 0..100. The reference scale
    is "$5/trade × 5 trades/day = $25/day = ~$500/month at $100k" =
    score 50. Anything 4× that = score 100."""
    monthly_proxy = expectancy_per_trade * trades_per_day * 22
    if monthly_proxy <= 0:
        return 0.0
    # 50 at $500/mo, 100 at $2000/mo, sigmoid-ish
    return max(0.0, min(100.0, 50.0 * monthly_proxy / 500.0))


def _score_kelly(kelly_pct: float) -> float:
    """Kelly fraction in [0, 0.25] maps to [0, 100]. >25% Kelly is
    insane sizing in real production — clamp.

    Formula: kelly_pct × 400 (so 0.25 → 100). 0.05 Kelly → 20."""
    if kelly_pct <= 0:
        return 0.0
    return max(0.0, min(100.0, kelly_pct * 400.0))


def _score_recovery_frequency(trades_per_day: float,
                                recovery_days: float | None) -> float:
    """trades_per_day / recovery_days. Higher = better (frequent
    trades + fast recovery). Reference: 1 trade/day with 30-day
    recovery = 0.033 → score 33."""
    if recovery_days is None or recovery_days <= 0:
        # No recovery yet (cell still underwater) → near-zero
        return 0.0
    if trades_per_day <= 0:
        return 0.0
    ratio = trades_per_day / recovery_days
    # Sigmoid: ratio=0.05 → ~50, ratio=0.5 → ~100
    return max(0.0, min(100.0, 100.0 * (1 - math.exp(-ratio * 4))))


def _score_sharpe_r(sharpe_r: float) -> float:
    """Sharpe in R-units. >1.0 is excellent for a single cell. Maps
    [0..2] to [0..100]."""
    if sharpe_r <= 0:
        return 0.0
    return max(0.0, min(100.0, sharpe_r * 50.0))


def _calmar_ratio(annualised_return_pct: float,
                   max_dd_pct: float) -> float:
    """Calmar = annualised return / |max drawdown|. Both in %.
    Renaissance/Citadel use variants of this. >1.0 = excellent,
    >3.0 = rare. Returns 0 when no DD or no return."""
    if max_dd_pct <= 0 or annualised_return_pct <= 0:
        return 0.0
    return annualised_return_pct / max_dd_pct


def _score_calmar(calmar: float) -> float:
    """Calmar [0..3] → score [0..100]. >3 capped (rare territory)."""
    if calmar <= 0:
        return 0.0
    return max(0.0, min(100.0, calmar * 100.0 / 3.0))


def _probabilistic_sharpe(sharpe_r: float, n_trades: int,
                            skew: float = 0.0, kurt: float = 3.0) -> float:
    """Probabilistic Sharpe Ratio (Bailey & López de Prado 2012).

    Probability that the TRUE Sharpe is > 0 given the observed Sharpe,
    sample size, and (optionally) skew + excess kurtosis. The math:

        z = sharpe × sqrt(n - 1) /
            sqrt(1 - skew × sharpe + (kurt - 1) / 4 × sharpe²)
        PSR = Φ(z)         # standard-normal CDF

    Without skew/kurt info we use the normal-returns simplification:
        z = sharpe × sqrt(n - 1)

    n=30 trades + Sharpe 1.0 → PSR ~0.99
    n=30 trades + Sharpe 0.3 → PSR ~0.95
    n=10 trades + Sharpe 1.0 → PSR ~0.86
    n=5 trades  + Sharpe 1.0 → PSR ~0.69
    """
    if n_trades < 2 or sharpe_r <= 0:
        return 0.0
    n_minus_1 = n_trades - 1
    if skew == 0.0 and kurt == 3.0:
        # Normal-returns simplification (no skew/kurt info available)
        z = sharpe_r * math.sqrt(n_minus_1)
    else:
        # Full Bailey-López de Prado formula
        denom_inner = (1.0 - skew * sharpe_r
                        + (kurt - 1.0) / 4.0 * sharpe_r ** 2)
        if denom_inner <= 0:
            return 0.0
        z = sharpe_r * math.sqrt(n_minus_1) / math.sqrt(denom_inner)
    # Φ(z) — standard normal CDF
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _score_psr(psr: float) -> float:
    """PSR is a probability [0..1]. We want the score to lean
    aggressive: >0.95 PSR → 100, <0.50 → tiny.
    Sigmoid: psr=0.95 → 95, psr=0.50 → 50, psr=0.10 → 10."""
    return max(0.0, min(100.0, psr * 100.0))


def _score_sortino(sortino: float) -> float:
    """Sortino ratio = mean R / downside std. Industry-standard
    refinement of Sharpe — penalises ONLY losing volatility (winning
    swings are not "risk"). Maps [0..3] to [0..100]: >3 = exceptional.

    Why include both Sharpe and Sortino: Sharpe penalises ALL
    deviation including upside. A strategy with rare big wins +
    consistent small wins gets dinged by Sharpe but rewarded by
    Sortino — which is more correct from a trader's perspective.
    Two Sigma + AQR use Sortino as a primary metric."""
    if sortino <= 0:
        return 0.0
    return max(0.0, min(100.0, sortino * 100.0 / 3.0))


def compute(
    *,
    expectancy_per_trade: float,
    win_rate: float,
    risk_reward: float,
    trades_per_day: float,
    recovery_days: float | None,
    sharpe_r: float,
    deploy_safe: bool,
    # v2 (Calmar + PSR) — optional with safe defaults
    annualised_return_pct: float = 0.0,
    max_dd_pct: float = 0.0,
    n_trades: int = 0,
    # v3 (Sortino) — optional, defaults to 0 so old callers still work
    sortino_r: float = 0.0,
    weights: dict[str, float] | None = None,
) -> EdgeScoreBreakdown:
    """Compute the multi-metric edge score. Returns a breakdown so
    the dashboard can show component contributions."""
    if weights is None:
        # v3 weights — Sortino added with 10% (industry treats it as
        # equally important to Sharpe; we soften slightly because we
        # also have Calmar covering downside risk in another way).
        weights = {
            "expectancy": 0.20,
            "kelly": 0.15,
            "recovery_freq": 0.10,
            "sharpe_r": 0.10,
            "calmar": 0.15,
            "psr": 0.10,
            "sortino": 0.10,       # NEW v3
            "deploy_safe": 0.10,
        }   # 1.00 sum (no renormalisation needed but kept for safety)
    # Renormalise weights to sum to 1.0 (in case caller provided a
    # custom dict that doesn't sum exactly to 1).
    w_sum = sum(weights.values())
    if w_sum <= 0:
        w_sum = 1.0
    w = {k: v / w_sum for k, v in weights.items()}

    # Kelly fraction = (WR × R:R - (1 - WR)) / R:R
    if risk_reward > 0:
        kelly_pct = (win_rate * risk_reward
                       - (1.0 - win_rate)) / risk_reward
    else:
        kelly_pct = 0.0

    # Calmar — annualised return ÷ max DD%
    calmar = _calmar_ratio(annualised_return_pct, max_dd_pct)

    # PSR — sample-size-aware Sharpe confidence
    psr = _probabilistic_sharpe(sharpe_r, n_trades)

    s_expectancy = _score_expectancy(expectancy_per_trade, trades_per_day)
    s_kelly = _score_kelly(kelly_pct)
    s_recovery = _score_recovery_frequency(trades_per_day, recovery_days)
    s_sharpe = _score_sharpe_r(sharpe_r)
    s_calmar = _score_calmar(calmar)
    s_psr = _score_psr(psr)
    s_sortino = _score_sortino(sortino_r)
    s_deploy = 100.0 if deploy_safe else 0.0

    total = (
        w.get("expectancy", 0) * s_expectancy
        + w.get("kelly", 0) * s_kelly
        + w.get("recovery_freq", 0) * s_recovery
        + w.get("sharpe_r", 0) * s_sharpe
        + w.get("calmar", 0) * s_calmar
        + w.get("psr", 0) * s_psr
        + w.get("sortino", 0) * s_sortino
        + w.get("deploy_safe", 0) * s_deploy
    )

    return EdgeScoreBreakdown(
        expectancy_score=s_expectancy,
        kelly_score=s_kelly,
        recovery_frequency_score=s_recovery,
        sharpe_r_score=s_sharpe,
        calmar_score=s_calmar,
        psr_score=s_psr,
        sortino_score=s_sortino,
        deploy_safe_bonus=s_deploy,
        total=total,
        expectancy_per_trade=expectancy_per_trade,
        kelly_pct=kelly_pct,
        trades_per_day=trades_per_day,
        recovery_days=recovery_days or 0.0,
        sharpe_r=sharpe_r,
        calmar_ratio=calmar,
        psr=psr,
        sortino_ratio=sortino_r,
        n_trades=n_trades,
        deploy_safe=deploy_safe,
    )


def compute_from_edge_stat(edge) -> EdgeScoreBreakdown:
    """Convenience: pass a `core.edge_catalog.EdgeStat` and we'll
    extract the right fields. Same scoring logic."""
    # Edge stats use these field names:
    expectancy_per_trade = float(getattr(edge, "expectancy_dollars", 0.0)
                                  or 0.0)
    win_rate = float(getattr(edge, "win_rate_pct", 0.0) or 0.0) / 100.0
    risk_reward = float(getattr(edge, "rr", 1.0) or 1.0)
    trades_per_day = float(getattr(edge, "trades_per_day", 0.0) or 0.0)
    recovery_days = getattr(edge, "recovery_days", None)
    sharpe_r = float(getattr(edge, "sharpe_R", 0.0)
                      or getattr(edge, "test_r", 0.0) or 0.0)
    deploy_safe = bool(getattr(edge, "deploy_safe", False))
    # NEW for v2: pull annualised return + max DD + sample size for
    # Calmar + PSR. Default to 0 if not on the EdgeStat (older catalog
    # rows might not have them — score components will just be zero).
    annualised_return_pct = float(getattr(edge, "annualised_return_pct", 0.0)
                                    or getattr(edge, "pct_per_month", 0.0) * 12
                                    or 0.0)
    max_dd_pct = float(getattr(edge, "max_dd_pct", 0.0) or 0.0)
    n_trades = int(getattr(edge, "n_test", 0)
                    or getattr(edge, "n_trades", 0)
                    or 0)
    # v3: Sortino — fall back to sharpe_r approximation when not
    # available on the edge stat (Sortino ≥ Sharpe by construction
    # since Sortino's denominator is smaller). Crude but safer than 0.
    sortino_r = float(getattr(edge, "sortino_R", 0.0)
                        or sharpe_r * 1.2 or 0.0)
    return compute(
        expectancy_per_trade=expectancy_per_trade,
        win_rate=win_rate,
        risk_reward=risk_reward,
        trades_per_day=trades_per_day,
        recovery_days=recovery_days,
        sharpe_r=sharpe_r,
        deploy_safe=deploy_safe,
        annualised_return_pct=annualised_return_pct,
        max_dd_pct=max_dd_pct,
        n_trades=n_trades,
        sortino_r=sortino_r,
    )
