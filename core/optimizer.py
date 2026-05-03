"""
optimizer.py — autonomous (strategy × ticker × tf × R:R × risk%) sweep
that ranks every cell by an FTMO-survival-weighted quality score.

The score answers a single trader's question:
   "If I started with $100k and risked 0.5% per trade, would this cell
    keep me alive long enough to pass an FTMO challenge?"

`score_cell` combines:
  • OOS profit factor + R       — does it actually have edge?
  • Max drawdown %             — penalises curves that breach FTMO -10%
  • FTMO Monte-Carlo P(pass)   — bootstrap survival
  • R:R ratio                   — bigger wins than losses?
  • Sample size                 — bigger pool → more trustworthy

Every component is a pure function so tests pin the shape without
needing real broker data. The actual sweep lives in
`scripts/optimize_portfolio.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from core.backtest_stats import FullStats


@dataclass(frozen=True)
class CellScore:
    """Single cell of the optimization grid."""
    strategy: str
    ticker: str
    tf: str
    rr_label: str               # '1:1', '1:2', '1:3', etc.
    stop_atr_mult: float
    target_atr_mult: float
    risk_pct: float
    n_test: int
    test_pf: float
    test_r: float
    win_rate: float
    max_dd_pct: float
    recovery_days: float | None
    max_consec_losses: int
    rr_ratio: float
    cagr_pct: float | None
    p_pass_30d: float | None    # FTMO simulation
    score: float                # Composite score (higher = better)
    sustained: bool             # Stayed above FTMO -10% floor on every bar
    notes: str = ""


def score_cell(stats: FullStats, *, p_pass: float | None,
               sustained: bool) -> float:
    """Composite quality score in [-∞, +∞] for ranking. Pure function.

    Components (all positive contributions for a 'good' cell):
      • test_pf clipped to [0, 5]                            (0–5)
      • test_R clipped to [-1, 2]                            (-1 – 2)
      • -max_dd_pct/10 (penalty grows with drawdown)         (-10 – 0)
      • rr_ratio clipped to [0, 3]                           (0–3)
      • log1p(n_test) — reward larger samples                (0–6)
      • +5 if sustained (never breached FTMO -10%)            (0/5)
      • +10 × p_pass (Monte-Carlo)                           (0–10)
      • -10 if max_consec_losses ≥ 5 (kills FTMO daily cap)    (0/-10)

    The constants are chosen so a 'great' cell scores ~25 and a 'bad'
    one scores well negative.
    """
    import math

    pf_capped = min(5.0, max(0.0, stats.profit_factor)
                     if stats.profit_factor != float("inf") else 5.0)
    r_capped = min(2.0, max(-1.0, stats.avg_R))
    dd_penalty = -stats.max_dd_pct / 10.0     # 30% DD → -3
    rr_capped = min(3.0, max(0.0, stats.risk_reward_ratio)
                     if stats.risk_reward_ratio != float("inf") else 3.0)
    sample_term = math.log1p(stats.n_trades)
    sustained_bonus = 5.0 if sustained else 0.0
    pass_term = 10.0 * (p_pass or 0.0)
    streak_penalty = -10.0 if stats.max_consec_losses >= 5 else 0.0

    return (pf_capped + r_capped + dd_penalty + rr_capped + sample_term
            + sustained_bonus + pass_term + streak_penalty)


def is_sustained(equity_curve, *, baseline: float,
                 floor_pct: float = 10.0) -> bool:
    """True if the equity curve never dropped below baseline × (1 - floor_pct/100).

    Used as a strict FTMO survival check: a cell that ever touched
    -10% from start would have failed the challenge regardless of
    eventual P&L."""
    if equity_curve is None or len(equity_curve) == 0:
        return True   # nothing to violate
    floor = baseline * (1.0 - floor_pct / 100.0)
    return bool((equity_curve["equity"] >= floor).all())


def rank_portfolio(cells: Iterable[CellScore],
                    *, top_n: int | None = 20,
                    require_sustained: bool = False
                    ) -> list[CellScore]:
    """Sort cells by score descending. If require_sustained, drop any
    cell that breached the FTMO total-loss floor."""
    rows = list(cells)
    if require_sustained:
        rows = [r for r in rows if r.sustained]
    rows.sort(key=lambda r: r.score, reverse=True)
    return rows[:top_n] if top_n else rows


# ---------------------------------------------------------------------------
# R:R config — the variants we sweep
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RRConfig:
    label: str
    stop_atr_mult: float
    target_atr_mult: float


RR_VARIANTS: list[RRConfig] = [
    RRConfig("1:1",   stop_atr_mult=1.5, target_atr_mult=1.5),
    RRConfig("1:1.5", stop_atr_mult=1.5, target_atr_mult=2.25),
    RRConfig("1:2",   stop_atr_mult=1.5, target_atr_mult=3.0),
    RRConfig("1:3",   stop_atr_mult=1.5, target_atr_mult=4.5),
    # Looser stop (more breathing room) — smaller R but fewer stop-outs
    RRConfig("1:2 wide", stop_atr_mult=2.5, target_atr_mult=5.0),
]


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(cells: list[CellScore], *, run_meta: str = "") -> str:
    """Pretty markdown table for `docs/optimization_<date>.md`."""
    lines = [
        "# Portfolio optimization — autonomous run",
        "",
        run_meta,
        "",
        "Score = profit_factor + R + R:R + sample-size + FTMO_pass_bonus",
        "       − drawdown_penalty − consec-loss_penalty",
        "",
        "Higher is better. `sustained` = equity never breached FTMO −10% floor.",
        "",
        "| rank | strategy | ticker | tf | R:R | n | PF | R | win% | maxDD% "
        "| recov_d | streak | rr | CAGR | P(pass) | sus | score |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, r in enumerate(cells, start=1):
        cagr = (f"{r.cagr_pct:+.1f}%" if r.cagr_pct is not None
                else "—")
        recov = (f"{r.recovery_days:.0f}" if r.recovery_days is not None
                  else "—")
        ppass = (f"{r.p_pass_30d*100:.0f}%" if r.p_pass_30d is not None
                  else "—")
        rr_disp = ("inf" if r.rr_ratio == float("inf")
                    else f"{r.rr_ratio:.2f}")
        pf_disp = ("inf" if r.test_pf == float("inf")
                    else f"{r.test_pf:.2f}")
        lines.append(
            f"| {i} | `{r.strategy}` | `{r.ticker}` | `{r.tf}` | {r.rr_label} "
            f"| {r.n_test} | {pf_disp} | {r.test_r:+.2f} | {r.win_rate:.0f} "
            f"| {r.max_dd_pct:.1f} | {recov} | {r.max_consec_losses} "
            f"| {rr_disp} | {cagr} | {ppass} "
            f"| {'✅' if r.sustained else '⛔'} | {r.score:.1f} |"
        )
    return "\n".join(lines) + "\n"
