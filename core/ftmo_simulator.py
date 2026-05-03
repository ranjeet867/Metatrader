"""
ftmo_simulator.py — Monte-Carlo bootstrap pass-rate for an FTMO test.

Given a portfolio of (strategy, ticker, tf, lots) configurations and each
strategy's OOS R-distribution, simulate `n_iterations` 30-day windows by
drawing trades-per-day from each strategy's bootstrap pool. Track:

  - cumulative balance vs daily cap (-5%)
  - cumulative balance vs total cap (-10%)
  - cumulative balance vs target (+10% phase 1, +5% phase 2)

Output:
  - P(pass), P(daily breach), P(total breach), P(no resolution)
  - Per-strategy contribution to expected return AND to max DD

INVARIANT-7: seeded RNG (default 42) so the same config always produces the
same simulator output.

Inputs are R-multiples (per-trade dollar PnL / initial dollar risk), not
dollar PnL — so the simulation is balance-relative and matches the way
FTMO measures its caps.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class StrategyDist:
    """A strategy's OOS R-multiple distribution (the bootstrap pool).

    Args:
        name: strategy display name.
        symbol: traded symbol — used in attribution rollups only.
        r_multiples: array-like of OOS per-trade R-multiples. The simulator
                     bootstraps from this pool with replacement.
        trades_per_day: expected trades/day for this slot. The simulator
                          draws Poisson(trades_per_day) trades on each
                          simulated day (capped at len(pool) for speed).
        risk_per_trade_pct: % of starting balance risked per trade. e.g. 1.0
                              means each draw of R contributes
                              R × 0.01 × starting_balance to the daily PnL.
    """
    name: str
    symbol: str
    r_multiples: np.ndarray
    trades_per_day: float = 0.5
    risk_per_trade_pct: float = 1.0


@dataclass
class FtmoSimResult:
    n_iterations: int
    p_pass: float
    p_daily_breach: float
    p_total_breach: float
    p_no_resolution: float
    expected_final_pct: float
    median_final_pct: float
    contrib_R_per_strategy: dict        # {strategy_name: mean R contribution}
    contrib_dd_per_strategy: dict        # {strategy_name: mean DD contribution}
    raw_finals: np.ndarray = field(default_factory=lambda: np.array([]))


def simulate_pass_rate(
    portfolio: list[StrategyDist],
    *,
    starting_balance: float = 100_000,
    days: int = 30,
    daily_loss_cap_pct: float = 5.0,
    total_loss_cap_pct: float = 10.0,
    pass_target_pct: float = 10.0,
    n_iterations: int = 10_000,
    seed: int = 42,
) -> FtmoSimResult:
    """Run the simulation. Returns FtmoSimResult.

    Args:
        portfolio: list of StrategyDist. An empty pool for a strategy means
                    no contribution.
        starting_balance: simulated starting balance.
        days: simulation horizon (default 30 = FTMO Phase 1).
        daily_loss_cap_pct: % below day_start_balance that breaches FTMO.
                              Default 5.0 (FTMO Phase 1).
        total_loss_cap_pct: % below starting_balance that breaches.
                              Default 10.0.
        pass_target_pct: % above starting_balance to mark "passed". 10.0 = Phase 1.
        n_iterations: Monte-Carlo iterations (default 10k → ~3% std error).
        seed: RNG seed (INVARIANT-7).
    """
    rng = np.random.default_rng(seed)

    n_strats = len(portfolio)
    if n_strats == 0:
        return FtmoSimResult(
            n_iterations=n_iterations, p_pass=0.0, p_daily_breach=0.0,
            p_total_breach=0.0, p_no_resolution=1.0,
            expected_final_pct=0.0, median_final_pct=0.0,
            contrib_R_per_strategy={}, contrib_dd_per_strategy={},
        )

    # Pre-build bootstrap arrays
    pools = []
    for s in portfolio:
        pool = np.asarray(s.r_multiples, dtype=float)
        if pool.size == 0:
            pool = np.array([0.0])    # neutral if no data
        pools.append(pool)

    risk_fracs = np.array([s.risk_per_trade_pct / 100.0 for s in portfolio])
    tp_day = np.array([s.trades_per_day for s in portfolio])

    finals = np.empty(n_iterations, dtype=float)
    n_pass = 0
    n_daily_breach = 0
    n_total_breach = 0
    n_resolved = 0
    contrib_R = {s.name: 0.0 for s in portfolio}
    contrib_dd = {s.name: 0.0 for s in portfolio}

    for it in range(n_iterations):
        balance = starting_balance
        peak = starting_balance
        per_strat_pnl = {s.name: 0.0 for s in portfolio}
        per_strat_max_dd = {s.name: 0.0 for s in portfolio}
        resolved = False
        outcome = "no_resolution"

        for _d in range(days):
            day_start = balance
            # Per-strategy daily PnL contribution
            for si, s in enumerate(portfolio):
                n_trades = rng.poisson(tp_day[si])
                if n_trades == 0:
                    continue
                draws = rng.choice(pools[si], size=int(n_trades), replace=True)
                # Each trade's $ PnL = R × (risk_per_trade_pct × day_start)
                trade_dollars = draws * risk_fracs[si] * day_start
                contribution = float(trade_dollars.sum())
                balance += contribution
                per_strat_pnl[s.name] += contribution
                # Track per-strategy intra-day drawdown
                day_running = day_start
                for c in trade_dollars:
                    day_running += c
                    dd = day_running - day_start
                    if dd < per_strat_max_dd[s.name]:
                        per_strat_max_dd[s.name] = dd
            # End of day: check caps
            daily_loss = (day_start - balance) / day_start * 100.0
            if daily_loss >= daily_loss_cap_pct:
                outcome = "daily_breach"
                resolved = True
                break
            total_loss = (starting_balance - balance) / starting_balance * 100.0
            if total_loss >= total_loss_cap_pct:
                outcome = "total_breach"
                resolved = True
                break
            ret_pct = (balance - starting_balance) / starting_balance * 100.0
            if ret_pct >= pass_target_pct:
                outcome = "pass"
                resolved = True
                break
            if balance > peak:
                peak = balance

        finals[it] = (balance - starting_balance) / starting_balance * 100.0
        if outcome == "pass":
            n_pass += 1
        elif outcome == "daily_breach":
            n_daily_breach += 1
        elif outcome == "total_breach":
            n_total_breach += 1
        if resolved:
            n_resolved += 1
        for name, p in per_strat_pnl.items():
            contrib_R[name] += p / starting_balance * 100.0
        for name, dd in per_strat_max_dd.items():
            contrib_dd[name] += dd / starting_balance * 100.0

    n = n_iterations
    return FtmoSimResult(
        n_iterations=n,
        p_pass=n_pass / n,
        p_daily_breach=n_daily_breach / n,
        p_total_breach=n_total_breach / n,
        p_no_resolution=(n - n_resolved) / n,
        expected_final_pct=float(finals.mean()),
        median_final_pct=float(np.median(finals)),
        contrib_R_per_strategy={k: v / n for k, v in contrib_R.items()},
        contrib_dd_per_strategy={k: v / n for k, v in contrib_dd.items()},
        raw_finals=finals,
    )


# ---------------------------------------------------------------------------
# Helper: load OOS R-distributions from the journal
# ---------------------------------------------------------------------------

def load_oos_distribution_from_db(db_path, *, strategy: str, symbol: str,
                                    mode: str = "backtest") -> np.ndarray:
    """Read R-multiples from data/v2.db filtered by (strategy, symbol, mode)."""
    from core import storage
    with storage.connect(db_path) as c:
        rows = c.execute(
            "SELECT r_multiple FROM trades "
            "WHERE strategy=? AND symbol=? AND mode=? "
            "AND r_multiple IS NOT NULL",
            (strategy, symbol, mode),
        ).fetchall()
    return np.array([r[0] for r in rows], dtype=float)
