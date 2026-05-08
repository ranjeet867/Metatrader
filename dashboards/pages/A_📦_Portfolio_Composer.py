"""
A_📦_Portfolio_Composer.py — pick a portfolio of strategies that hits a
daily-trade target with risk allocated by data.

Goal: 1-5 trades/day combined, high P(pass), low correlation across
tickers/TFs, sparse strategies compensated with higher R:R + win-rate
filters. The page auto-optimises the best 8-12 cell mix and computes
the per-strategy lot/risk allocation that lands on the user's daily-loss
budget.

Sections:
  1. Sidebar — daily trade target, FTMO daily cap %, account equity.
  2. Auto-optimize button — picks top N cells via score + dedupe by
     (strategy_name, ticker), preferring different TFs and tickers for
     diversification.
  3. Live composer table — checkboxes per cell, computed combined
     trades/day, daily risk budget, suggested risk %/trade per cell.
  4. Deploy button — writes selected cells to deployments.json with the
     computed risk %/trade and daily cap.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core import account_manager                                # noqa: E402
from core import cost_defaults                                  # noqa: E402
from core import deployment as dep_mod                          # noqa: E402
from core import edge_catalog                                   # noqa: E402
from core.parity_gate import ParityGate                         # noqa: E402
from dashboards.components import theme                         # noqa: E402

DB_PATH = REPO / "data" / "v2.db"


def _resolve_base_strategy(variant_name: str,
                              registered: dict) -> str | None:
    """Thin shim around the shared resolver in strategy_resolver.py."""
    from dashboards.components.strategy_resolver import resolve_base_strategy
    return resolve_base_strategy(variant_name, registered)


def _flatten_catalog() -> list[edge_catalog.EdgeStat]:
    cat = edge_catalog.load_catalog()
    out = []
    for rows in cat.values():
        out.extend(rows)
    return [e for e in out if e.net_pnl_dollars != 0
             or e.max_dd_dollars != 0]


def _classify_speed(tpd: float | None) -> str:
    if tpd is None:
        return "—"
    if tpd >= 1.5:
        return "🚀 high (≥1.5/day)"
    if tpd >= 0.5:
        return "🚶 medium (0.5-1.5/day)"
    if tpd >= 0.1:
        return "🐢 slow (0.1-0.5/day)"
    return "❄ rare (<0.1/day)"


def _auto_optimize(edges: list[edge_catalog.EdgeStat],
                   *, target_trades_per_day: float,
                   max_strategies: int = 12,
                   min_pass_rate: float = 0.70,
                   min_n_test: int = 10,
                   max_recovery_days: float = 200.0,
                   trendo_zone_only: bool = True,
                   ) -> list[edge_catalog.EdgeStat]:
    """Pick a portfolio that approximates target_trades_per_day combined
    while:
      • each cell has p_pass ≥ min_pass_rate
      • each cell has n_test ≥ min_n_test
      • each cell has recovery_days ≤ max_recovery_days (FTMO mental floor)
      • diversifies across (strategy, ticker, tf, side) tuples
      • prioritises cells with high score AND large OOS sample
      • mixes one slow + several medium/high cells

    Returns up to max_strategies cells.
    """
    qualified = [
        e for e in edges
        if e.p_pass_30d is not None
        and e.p_pass_30d >= min_pass_rate
        and e.n_test >= min_n_test
        and e.test_r > 0
        and e.trades_per_day is not None
        # Long-recovery cells are mentally brutal during FTMO challenges.
        # Drop anything with recovery > max_recovery_days. None means
        # never recovered → also dropped.
        and (e.recovery_days is not None
              and e.recovery_days <= max_recovery_days)
        # Trendo R:R × Win-Rate criterion: only keep cells where the
        # math is profitable in the long run (EV per R > 0).
        and (not trendo_zone_only or e.is_trendo_profitable)
        # HARD GATES — final bouncer. A cell with overall PF<1.05,
        # negative TEST partition, or "Recovery: not yet" is *never*
        # auto-picked, even if score / WR look good. The score itself
        # is already capped at 15 for failing cells, but we double-gate
        # here so a future score-formula change can't accidentally
        # regress this safety check.
        and e.deploy_safe
    ]
    if not qualified:
        return []
    # Phase-32: rank by Edge Score (multi-metric: Kelly + expectancy +
    # recovery×freq + Sharpe-R + Calmar + PSR + Sortino + deploy_safe).
    # Replaces the legacy `e.score` heuristic which was just PF-biased.
    # Edge Score surfaces TRUE deployable cells — steady winners over
    # fragile high-PF flukes. Falls back to legacy `score` when
    # edge_score computation fails.
    from core import edge_score as _es

    def _ranking_key(e):
        try:
            return -_es.compute_from_edge_stat(e).total
        except Exception:
            return -float(getattr(e, "score", 0) or 0)

    qualified.sort(key=_ranking_key)

    picked: list[edge_catalog.EdgeStat] = []
    seen_keys: set[tuple[str, str]] = set()
    combined_tpd = 0.0
    for e in qualified:
        key = (e.strategy, e.ticker)   # dedupe — one cell per (strat, ticker)
        if key in seen_keys:
            continue
        if len(picked) >= max_strategies:
            break
        # Add cells until combined trades/day exceeds 1.5× target — gives
        # us room to drop low-confidence ones.
        if combined_tpd >= target_trades_per_day * 1.5 and len(picked) >= 6:
            break
        picked.append(e)
        seen_keys.add(key)
        combined_tpd += e.trades_per_day or 0.0
    return picked


def _suggest_risk_per_trade(edges: list[edge_catalog.EdgeStat],
                              *, daily_cap_pct: float) -> dict[str, float]:
    """Allocate the daily-loss cap budget across the cells.

    Risk per cell = (daily_cap_pct / sum_of_expected_daily_losses) ×
                     expected_daily_loss_per_cell

    Where expected_daily_loss_per_cell ≈ trades_per_day × (1 - win_rate)
                                          × stop_distance_R × 1R.
    Simplified: rare-but-high-WR cells get bigger size (since they don't
    fire often, when they do it's high quality). Frequent low-WR cells
    get smaller size.

    Output: {deployment_slug: suggested_risk_pct}.
    """
    if not edges or daily_cap_pct <= 0:
        return {}
    # Score per cell — frequency × loss probability × baseline 1R risk
    scores: dict[str, float] = {}
    for e in edges:
        tpd = e.trades_per_day or 0.0
        loss_rate = 1.0 - (e.win_rate_pct / 100.0)
        # Expected daily-1R loss exposure
        scores[e.strategy + "__" + e.ticker + "__" + e.tf] = (
            tpd * loss_rate
        )
    total = sum(scores.values()) or 1.0
    # Inverse scale: rare cells get more risk %
    out: dict[str, float] = {}
    for e in edges:
        slug = e.strategy + "__" + e.ticker + "__" + e.tf
        share = scores[slug] / total
        # Per-cell risk = daily_cap × share, but with a floor of 0.1%
        risk_pct = max(0.10, min(2.0, daily_cap_pct * share))
        out[slug] = risk_pct
    # Re-normalise so the sum of (risk × tpd × loss_rate) ≈ daily_cap_pct
    expected_total = sum(
        out[e.strategy + "__" + e.ticker + "__" + e.tf]
        * (e.trades_per_day or 0.0)
        * (1 - e.win_rate_pct / 100.0)
        for e in edges
    )
    if expected_total > 0:
        scale = daily_cap_pct / expected_total
        out = {k: max(0.10, min(2.0, v * scale)) for k, v in out.items()}
    return out


def _autotune_risk_for_stress(
    edges: list[edge_catalog.EdgeStat],
    base_alloc: dict[str, float],
    *,
    target_dd_pct: float,
    floor_pct: float = 0.10,
    ceiling_pct: float = 2.0,
) -> tuple[dict[str, float], float, dict]:
    """Scale the per-cell risk allocation so the WORST-CASE stress-test
    drawdown sits at or below `target_dd_pct`.

    The independent-streak drawdown is sqrt(sum((r_i × L_i)²)) and the
    simultaneous case is sum(r_i × L_i). Both are LINEAR in r_i, so a
    single multiplicative scale k applied to every r_i scales each
    scenario by exactly k. We pick k = target / max(simul, indep) and
    clamp to [0, 1] (we only ever shrink, never enlarge).

    Returns (new_alloc, scale_factor, stress_after) where stress_after is
    the post-tune {simul_pct, indep_pct, max_consec, ...} dict.
    """
    import math
    if not edges or not base_alloc:
        return dict(base_alloc), 1.0, {
            "simul_pct": 0.0, "indep_pct": 0.0, "max_consec": 0,
        }
    # Compute current worst case
    rows = []
    simul = 0.0
    sos = 0.0
    max_consec = 0
    for e in edges:
        slug = e.strategy + "__" + e.ticker + "__" + e.tf
        r = float(base_alloc.get(slug, 0.30))
        L = int(e.max_consec_losses or 0)
        streak = r * L
        simul += streak
        sos += streak ** 2
        max_consec = max(max_consec, L)
        rows.append((slug, r, L))
    indep = math.sqrt(sos)
    worst = max(simul, indep)
    if worst <= 0:
        # Nothing to scale — every cell has L=0 (no historical losers).
        return dict(base_alloc), 1.0, {
            "simul_pct": simul, "indep_pct": indep, "max_consec": max_consec,
        }
    # Scaling factor — only shrink (k ≤ 1). If already under target, leave
    # the allocation alone so the user keeps their full upside.
    k = min(1.0, target_dd_pct / worst)
    new_alloc: dict[str, float] = {}
    for slug, r, L in rows:
        new_alloc[slug] = max(floor_pct, min(ceiling_pct, r * k))
    # Recompute stress after scaling for the report
    new_simul = sum(new_alloc[s] * L for s, _, L in rows)
    new_sos = sum((new_alloc[s] * L) ** 2 for s, _, L in rows)
    new_indep = math.sqrt(new_sos)
    return new_alloc, k, {
        "simul_pct": new_simul,
        "indep_pct": new_indep,
        "max_consec": max_consec,
    }


def _apply_risk_mode(
    edges: list[edge_catalog.EdgeStat],
    base_alloc: dict[str, float],
    *,
    mode: str,                      # "recovery" | "normal" | "peak"
    current_equity: float,
    baseline_equity: float,
    max_loss_today_dollars: float,
    min_trades_today: int,
    peak_multiplier: float,
    floor_pct: float = 0.10,
    ceiling_pct: float = 2.0,
) -> tuple[dict[str, float], dict]:
    """Apply equity-aware risk scaling to the base allocation.

    Recovery mode (account in drawdown):
        Per-trade $ risk is capped at:
            $/trade ≤ max_loss_today_dollars / min_trades_today
        Each cell's risk_pct is THEN clamped to:
            min(base, $/trade / current_equity × 100)
        This ensures even if every cell fires today and stops out, total
        loss ≤ max_loss_today_dollars.

    Normal mode: leave the base allocation alone (Trendo composer +
    stress autotune already applied upstream).

    Peak mode (account at new high): scale every cell by `peak_multiplier`
    (default 1.5×), still clamped to [floor_pct, ceiling_pct].

    Returns (new_alloc, info) where info has explanatory fields for the UI.
    """
    info = {
        "mode": mode,
        "current_equity": current_equity,
        "baseline_equity": baseline_equity,
        "dd_pct": (
            (1.0 - current_equity / baseline_equity) * 100.0
            if baseline_equity > 0 else 0.0
        ),
        "applied": False,
        "cap_dollars_per_trade": None,
        "cap_risk_pct": None,
        "multiplier": 1.0,
    }
    if not edges or not base_alloc:
        return dict(base_alloc), info

    if mode == "recovery":
        # $ cap per trade — straight division, simple and easy to explain
        cap_dollars = (max_loss_today_dollars
                          / max(1, int(min_trades_today)))
        cap_pct = cap_dollars / current_equity * 100.0 if current_equity > 0 else 0.0
        info["cap_dollars_per_trade"] = cap_dollars
        info["cap_risk_pct"] = cap_pct
        info["applied"] = True
        new_alloc = {}
        for slug, r in base_alloc.items():
            new_alloc[slug] = max(floor_pct,
                                    min(ceiling_pct, min(r, cap_pct)))
        return new_alloc, info

    if mode == "peak":
        info["multiplier"] = peak_multiplier
        info["applied"] = True
        new_alloc = {
            slug: max(floor_pct, min(ceiling_pct, r * peak_multiplier))
            for slug, r in base_alloc.items()
        }
        return new_alloc, info

    # Normal — no change
    return dict(base_alloc), info


def _portfolio_table(edges: list[edge_catalog.EdgeStat],
                      risk_alloc: dict[str, float]) -> pd.DataFrame:
    """Build the candidate-pool DataFrame.

    Phase-32: added "Edge Score" column from `core.edge_score` —
    multi-metric scoring (Kelly + expectancy + recovery×frequency +
    Sharpe-R + deploy_safe). Strictly better ranking signal than the
    legacy heuristic Score (kept as "Score (legacy)" for comparison).
    Sortable in the data_editor — users can rank by Edge Score and
    pick top-N for the portfolio.
    """
    from core import edge_score as _es
    rows = []
    for e in edges:
        slug = e.strategy + "__" + e.ticker + "__" + e.tf
        tpd = e.trades_per_day or 0.0
        rec = e.recovery_days
        # Recovery flag: 🟢 ≤90d, 🟡 90-200d, 🔴 >200d, ⛔ never recovered
        if rec is None:
            rec_flag = "⛔ never"
        elif rec <= 90:
            rec_flag = f"🟢 {rec:.0f}d"
        elif rec <= 200:
            rec_flag = f"🟡 {rec:.0f}d"
        else:
            rec_flag = f"🔴 {rec:.0f}d"
        # Compute the multi-metric edge score
        try:
            es_breakdown = _es.compute_from_edge_stat(e)
            edge_score_total = round(es_breakdown.total, 1)
        except Exception:
            edge_score_total = 0.0
        rows.append({
            "Strategy": e.strategy,
            "Ticker": e.ticker,
            "TF": e.tf,
            "Side": e.side,
            "R:R": e.rr_label or "—",
            "Speed": _classify_speed(tpd),
            "Trades/day": round(tpd, 2),
            "Trades/month": round(e.trades_per_month or 0, 1),
            "Win%": round(e.win_rate_pct, 1),
            "rr": round(e.rr_ratio, 2) if e.rr_ratio else None,
            "Max consec L": e.max_consec_losses,
            "Recovery": rec_flag,
            "P(pass)": (f"{e.p_pass_30d*100:.0f}%"
                        if e.p_pass_30d is not None else "—"),
            "Suggested risk %": round(risk_alloc.get(slug, 0.30), 2),
            "Edge Score": edge_score_total,
            "Score (legacy)": round(e.score, 1),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Worst-case stress test
# ---------------------------------------------------------------------------

def _stress_test_metrics(edges: list[edge_catalog.EdgeStat],
                          risk_alloc: dict[str, float]) -> dict:
    """Compute three drawdown scenarios:

      A. SIMULTANEOUS — every cell hits its full max-consecutive-losing
         streak in the same window. Sum of (risk × L). This is the
         disaster case (correlation = +1.0).
      B. INDEPENDENT — streaks are uncorrelated. sqrt(sum((risk × L)²)).
         Closer to reality if cells are on different tickers / TFs.
      C. AVG STREAK — expected daily risk × longest L. A middle estimate.

    Also returns per-cell streak data for the breakdown table.
    """
    import math
    rows = []
    sum_streak_pct = 0.0
    sos = 0.0
    max_consec = 0
    max_recov_d = 0.0
    sum_recov_d = 0.0
    n_recovered = 0
    for e in edges:
        slug = e.strategy + "__" + e.ticker + "__" + e.tf
        risk = float(risk_alloc.get(slug, 0.30))
        L = int(e.max_consec_losses or 0)
        tpd = float(e.trades_per_day or 0)
        # Worst-case streak loss assumes every loss is a full -1R stop.
        # That's the conservative read; many losses come in early at
        # smaller fractions, but for stress-test we assume the worst.
        streak_pct = risk * L
        streak_days = (L / tpd) if tpd > 0 else float("inf")
        sum_streak_pct += streak_pct
        sos += streak_pct ** 2
        max_consec = max(max_consec, L)
        rd = e.recovery_days
        if rd is not None:
            sum_recov_d += float(rd)
            max_recov_d = max(max_recov_d, float(rd))
            n_recovered += 1
        rows.append({
            "strategy": e.strategy,
            "ticker": e.ticker,
            "tf": e.tf,
            "risk_pct": risk,
            "consec_L": L,
            "tpd": tpd,
            "streak_pct": streak_pct,
            "streak_days": streak_days,
            "recov_d": rd,
        })
    indep_pct = math.sqrt(sos)
    return {
        "rows": rows,
        "simul_pct": sum_streak_pct,
        "indep_pct": indep_pct,
        "max_consec": max_consec,
        "max_recov_d": max_recov_d,
        "sum_recov_d": sum_recov_d,
        "n_recovered": n_recovered,
        "n_cells": len(edges),
    }


def _render_stress_test(edges: list[edge_catalog.EdgeStat],
                          risk_alloc: dict[str, float],
                          expected_daily_risk_pct: float,
                          ftmo_floor_pct: float = 10.0,
                          ftmo_daily_pct: float = 5.0) -> None:
    """Render a panel showing worst-case drawdowns and recovery time."""
    if not edges:
        return
    m = _stress_test_metrics(edges, risk_alloc)
    avg_streak_pct = expected_daily_risk_pct * m["max_consec"]

    # Headline metrics
    st.markdown("### 🛡️  Worst-case stress test")
    st.caption(
        "If your strategies hit their longest historical losing streaks "
        "**at the same time**, where does the portfolio end up?  "
        "*Simultaneous* assumes correlation +1 (all cells streak together — "
        "rare but possible during regime shifts). *Independent* assumes "
        "uncorrelated streaks (sqrt-sum-of-squares, more realistic across "
        "different tickers/TFs). *Avg streak* multiplies the headline "
        "expected daily loss by the longest L in the portfolio."
    )

    cols = st.columns(4)

    def _flag(pct: float) -> str:
        if pct >= ftmo_floor_pct:
            return "⛔"
        if pct >= ftmo_floor_pct * 0.7:
            return "🟡"
        return "✅"

    cols[0].metric(
        f"{_flag(m['simul_pct'])} Simultaneous (worst)",
        f"-{m['simul_pct']:.2f}%",
        delta=f"vs −{ftmo_floor_pct:.0f}% FTMO floor",
        delta_color="inverse" if m["simul_pct"] >= ftmo_floor_pct
        else "normal",
        help="Every strategy hits its full max-consec-losing streak in the "
              "same window. Sum of (suggested_risk × L) across all cells.",
    )
    cols[1].metric(
        f"{_flag(m['indep_pct'])} Independent (uncorrelated)",
        f"-{m['indep_pct']:.2f}%",
        delta=f"vs −{ftmo_floor_pct:.0f}% FTMO floor",
        delta_color="inverse" if m["indep_pct"] >= ftmo_floor_pct
        else "normal",
        help="Streaks are independent across cells: "
              "sqrt(sum((risk × L)²)). More realistic when strategies "
              "trade different tickers / timeframes.",
    )
    cols[2].metric(
        f"{_flag(avg_streak_pct)} Avg streak",
        f"-{avg_streak_pct:.2f}%",
        delta=f"daily {expected_daily_risk_pct:.2f}% × L={m['max_consec']}",
        delta_color="inverse" if avg_streak_pct >= ftmo_floor_pct
        else "normal",
        help="Expected daily risk × longest historical losing streak. A "
              "middle estimate of what a bad week might look like.",
    )
    if m["n_recovered"] == 0:
        cols[3].metric("Recovery time", "n/a",
                          help="No cells have ever recovered from their "
                                "max DD in the historical sample.")
    else:
        cols[3].metric(
            "Recovery time (worst single)",
            f"{m['max_recov_d']:.0f}d",
            delta=f"{m['n_recovered']}/{m['n_cells']} cells with data",
            delta_color="off",
            help="How long the slowest-recovering cell took to claw back "
                  "from its worst historical drawdown to a new equity high. "
                  "Sequential pessimistic = "
                  f"{m['sum_recov_d']:.0f}d.",
        )

    # Verdict line
    if m["simul_pct"] >= ftmo_floor_pct or m["indep_pct"] >= ftmo_floor_pct:
        offenders = sorted(
            m["rows"], key=lambda r: -r["streak_pct"]
        )[:2]
        offender_str = ", ".join(
            f"`{r['strategy']}` ({r['streak_pct']:.1f}% / "
            f"L={r['consec_L']})"
            for r in offenders
        )
        st.error(
            f"⛔ **Even uncorrelated, this portfolio busts FTMO −"
            f"{ftmo_floor_pct:.0f}%.**  "
            f"Top contributors: {offender_str}. "
            f"Cut suggested risk on these cells (a 50% trim on a 2% cell "
            f"halves its streak loss from 8% to 4%) or remove them "
            f"entirely. Recovery from a busted account = forever — much "
            f"worse than the {m['max_recov_d']:.0f}-day historical "
            f"recovery you'd face on a survivable drawdown."
        )
    elif avg_streak_pct >= ftmo_floor_pct * 0.7:
        st.warning(
            f"🟡 Avg streak ({avg_streak_pct:.2f}%) is uncomfortably close "
            f"to the {ftmo_floor_pct:.0f}% floor. Headroom is thin — "
            f"consider trimming the highest-risk cells or lowering the "
            f"daily cap below {ftmo_daily_pct:.0f}%."
        )
    else:
        st.success(
            f"✅ Portfolio survives all three stress scenarios under the "
            f"FTMO −{ftmo_floor_pct:.0f}% floor. "
            f"Simul {m['simul_pct']:.1f}% · indep {m['indep_pct']:.1f}% · "
            f"avg streak {avg_streak_pct:.1f}%. Worst single recovery: "
            f"{m['max_recov_d']:.0f}d."
        )

    # Per-cell streak breakdown (collapsed by default)
    with st.expander(
        f"Per-cell streak breakdown ({len(edges)} cells)",
        expanded=False,
    ):
        breakdown = pd.DataFrame([
            {
                "Strategy": r["strategy"],
                "Ticker": r["ticker"],
                "TF": r["tf"],
                "Risk %": round(r["risk_pct"], 2),
                "Max consec L": r["consec_L"],
                "Trades/day": round(r["tpd"], 2),
                "Streak loss": f"-{r['streak_pct']:.2f}%",
                "Streak duration": (
                    f"{r['streak_days']:.1f}d"
                    if r["streak_days"] != float("inf")
                    else "n/a"
                ),
                "Recovery": (
                    f"{r['recov_d']:.0f}d"
                    if r["recov_d"] is not None
                    else "never recovered"
                ),
            }
            for r in m["rows"]
        ])
        st.dataframe(breakdown, width="stretch",
                       height=min(400, 38 * (len(breakdown) + 1)))


def _run_parity_for_dialog(edge, base_name, gate) -> None:
    """Run parity for one strategy from inside the deploy dialog and
    record the pass if successful. Uses the resolved base name and the
    SAME time-guard config for both engines (via core.parity_check)."""
    import time
    from core.config import load_config
    from core.data import load_parquet
    from core.parity_check import run_parity
    from core.symbol_info_loader import try_load as try_load_symbol_info
    from dashboards.components.state import (
        DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit, discover_strategies,
    )

    strategies = discover_strategies()
    if base_name not in strategies:
        st.error(f"⛔ Base strategy `{base_name}` not registered.")
        return
    StratCls, ParamsCls = strategies[base_name]
    parquet = REPO / "data" / f"{edge.ticker}_{edge.tf}.parquet"
    if not parquet.exists():
        st.error(f"⛔ No parquet for {edge.ticker} {edge.tf}.")
        return
    cfg = load_config()
    df = load_parquet(parquet)
    try:
        strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    except Exception as e:
        st.error(f"⛔ Could not instantiate `{base_name}`: {e}")
        return
    sym_info = try_load_symbol_info(edge.ticker)
    t0 = time.time()
    pr = run_parity(
        df, strat, symbol=edge.ticker, tf=edge.tf,
        starting_balance=100_000,
        lots=DEFAULT_LOTS.get(edge.ticker, 0.1),
        money_per_unit_price=resolve_money_per_unit(edge.ticker),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        risk_cfg=cfg, symbol_info=sym_info,
    )
    elapsed = time.time() - t0
    div = pr.divergence_dollars
    if div < 0.01:
        gate.record_pass(base_name, divergence_dollars=div)
        st.toast(f"✅ Parity recorded for {base_name} ({elapsed:.1f}s)")
    else:
        st.toast(f"⛔ Parity FAILED for {base_name}: ${div:.2f} divergence",
                  icon="⛔")


def _run_parity_inline(edge: edge_catalog.EdgeStat) -> None:
    """Run replay-parity for a single edge, store the result in session,
    and ask Streamlit to rerun so the result panel renders. Uses the
    centralised core.parity_check helper so backtest and replay see the
    SAME time-guard config (no_entry window, daily-flat, weekend-flat)."""
    import time
    from core.config import load_config
    from core.data import load_parquet
    from core.parity_check import run_parity
    from core.symbol_info_loader import try_load as try_load_symbol_info
    from dashboards.components.state import (
        DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit, discover_strategies,
    )

    strategies = discover_strategies()
    base_strat_name = _resolve_base_strategy(edge.strategy, strategies)
    if base_strat_name is None:
        st.session_state["_pc_action_result"] = {
            "kind": "parity",
            "status": "skip",
            "msg": f"could not map variant `{edge.strategy}` to a "
                    f"registered base. Registered: "
                    f"{sorted(strategies.keys())}",
        }
        st.rerun()
        return

    StratCls, ParamsCls = strategies[base_strat_name]
    parquet = REPO / "data" / f"{edge.ticker}_{edge.tf}.parquet"
    if not parquet.exists():
        st.session_state["_pc_action_result"] = {
            "kind": "parity",
            "status": "skip",
            "msg": f"no parquet for {edge.ticker}_{edge.tf}",
        }
        st.rerun()
        return

    cfg = load_config()
    df = load_parquet(parquet)
    try:
        strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    except Exception as e:
        st.session_state["_pc_action_result"] = {
            "kind": "parity", "status": "skip",
            "msg": f"instantiate failed: {e}",
        }
        st.rerun()
        return

    sym_info = try_load_symbol_info(edge.ticker)
    t0 = time.time()
    pr = run_parity(
        df, strat, symbol=edge.ticker, tf=edge.tf,
        starting_balance=100_000,
        lots=DEFAULT_LOTS.get(edge.ticker, 0.1),
        money_per_unit_price=resolve_money_per_unit(edge.ticker),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        risk_cfg=cfg, symbol_info=sym_info,
    )
    elapsed = time.time() - t0
    bt, rp, div = pr.bt, pr.rp, pr.divergence_dollars
    if div < 0.01:
        ParityGate(DB_PATH).record_pass(base_strat_name,
                                          divergence_dollars=div)
        status = "pass"
    else:
        status = "fail"
    st.session_state["_pc_action_result"] = {
        "kind": "parity", "status": status,
        "strategy": base_strat_name, "variant": edge.strategy,
        "ticker": edge.ticker, "tf": edge.tf,
        "n_bars": len(df),
        "first_bar": str(df["time"].iloc[0]),
        "last_bar": str(df["time"].iloc[-1]),
        "bt_trades": bt.n_trades, "rp_trades": rp.n_trades,
        "bt_pnl": bt.sum_realized_pnl, "rp_pnl": rp.sum_realized_pnl,
        "divergence": div, "elapsed_s": elapsed,
    }
    st.rerun()


def _run_backtest_inline(edge: edge_catalog.EdgeStat) -> None:
    """Run a backtest for the cell and store full stats in session."""
    import time
    from core.backtest import partition_train_test, run_backtest
    from core.backtest_stats import compute_full_stats
    from core.data import load_parquet
    from dashboards.components.state import (
        DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit, discover_strategies,
    )

    strategies = discover_strategies()
    base = _resolve_base_strategy(edge.strategy, strategies)
    if base is None:
        st.session_state["_pc_action_result"] = {
            "kind": "backtest", "status": "skip",
            "msg": f"could not map variant `{edge.strategy}` to a "
                    f"registered base. Registered: "
                    f"{sorted(strategies.keys())}",
        }
        st.rerun()
        return

    StratCls, ParamsCls = strategies[base]
    parquet = REPO / "data" / f"{edge.ticker}_{edge.tf}.parquet"
    if not parquet.exists():
        st.session_state["_pc_action_result"] = {
            "kind": "backtest", "status": "skip",
            "msg": f"no parquet for {edge.ticker}_{edge.tf}",
        }
        st.rerun()
        return

    df = load_parquet(parquet)
    strat = StratCls() if ParamsCls is None else StratCls(ParamsCls())
    t0 = time.time()
    result = run_backtest(
        df, strat.signals(df), starting_balance=100_000,
        lots=DEFAULT_LOTS.get(edge.ticker, 0.1),
        money_per_unit_price=resolve_money_per_unit(edge.ticker),
        commission_per_trade=3.0, slippage_per_fill_atr_frac=0.1,
        symbol=edge.ticker,
        enforce_daily_flat=True,
    )
    elapsed = time.time() - t0
    stats = compute_full_stats(result, starting_balance=100_000)
    train, test = partition_train_test(result, 0.6, n_bars=len(df))
    st.session_state["_pc_action_result"] = {
        "kind": "backtest", "status": "ok",
        "strategy": base, "ticker": edge.ticker, "tf": edge.tf,
        "n_bars": len(df), "elapsed_s": elapsed,
        "stats": stats,
        "train_n": train.n_trades, "test_n": test.n_trades,
        "train_pf": train.profit_factor, "test_pf": test.profit_factor,
        "train_r": train.avg_R, "test_r": test.avg_R,
    }
    st.rerun()


def _render_action_result(result: dict) -> None:
    """Render whichever action result is in session_state inline."""
    st.markdown("---")
    kind = result.get("kind")
    if kind == "parity":
        _render_parity_result(result)
    elif kind == "backtest":
        _render_backtest_result(result)


def _render_parity_result(r: dict) -> None:
    st.markdown("#### 🔬  Parity result")
    if r["status"] == "skip":
        st.warning(f"⚠ Skipped: {r['msg']}")
        return
    ok = r["status"] == "pass"
    color = "#15803d" if ok else "#7f1d1d"
    label = "✅ PARITY PASSED" if ok else "⛔ PARITY FAILED"
    st.markdown(
        f"<div style='display:inline-block;background:{color};color:white;"
        f"padding:6px 12px;border-radius:12px;font-weight:700;"
        f"font-family:ui-monospace,Menlo,monospace;'>{label}</div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"`{r['variant']}` (base: `{r['strategy']}`) on `{r['ticker']}` "
        f"`{r['tf']}` — {r['n_bars']} bars  ·  "
        f"{r['first_bar'][:10]} → {r['last_bar'][:10]}  ·  "
        f"elapsed {r['elapsed_s']:.2f}s"
    )
    cols = st.columns(4)
    cols[0].metric("BT trades", r["bt_trades"])
    cols[1].metric("Replay trades", r["rp_trades"],
                      delta=r["rp_trades"] - r["bt_trades"])
    cols[2].metric("BT $", f"${r['bt_pnl']:+,.4f}")
    cols[3].metric("Replay $", f"${r['rp_pnl']:+,.4f}",
                      delta=f"${r['divergence']:.6f} divergence",
                      delta_color=("normal" if ok else "inverse"))


def _render_backtest_result(r: dict) -> None:
    st.markdown("#### 📊  Backtest result")
    if r["status"] == "skip":
        st.warning(f"⚠ Skipped: {r['msg']}")
        return
    s = r["stats"]
    st.caption(
        f"`{r['strategy']}` on `{r['ticker']}` `{r['tf']}` — "
        f"{r['n_bars']} bars · elapsed {r['elapsed_s']:.2f}s"
    )
    cols = st.columns(5)
    cols[0].metric("Trades", s.n_trades)
    cols[1].metric("Win%", f"{s.win_rate_pct:.1f}%",
                      delta=f"{s.n_wins}W / {s.n_losses}L")
    pf_disp = ("inf" if s.profit_factor == float("inf")
                else f"{s.profit_factor:.2f}")
    cols[2].metric("Profit factor", pf_disp)
    cols[3].metric("Avg R", f"{s.avg_R:+.2f}")
    cols[4].metric("Net P&L", f"${s.sum_realized:+,.0f}")

    cols2 = st.columns(5)
    cols2[0].metric("Max DD", f"{s.max_dd_pct:.1f}%",
                       delta=f"-${s.max_dd_dollars:,.0f}",
                       delta_color="inverse")
    cols2[1].metric("DD duration",
                       f"{s.max_dd_duration_days:.0f}d")
    cols2[2].metric("Recovery",
                       "n/a" if s.recovery_duration_days is None
                       else f"{s.recovery_duration_days:.0f}d")
    cols2[3].metric("Avg win", f"${s.avg_win_dollars:+,.0f}")
    cols2[4].metric("Avg loss", f"${s.avg_loss_dollars:+,.0f}",
                       delta_color="inverse")

    train_pf = r['train_pf']
    test_pf = r['test_pf']
    train_pf_s = "inf" if train_pf == float('inf') else f"{train_pf:.2f}"
    test_pf_s = "inf" if test_pf == float('inf') else f"{test_pf:.2f}"
    st.caption(
        f"**Train (60%)** n={r['train_n']} · "
        f"PF {train_pf_s} · R {r['train_r']:+.2f}     "
        f"**Test (40%)** n={r['test_n']} · "
        f"PF {test_pf_s} · R {r['test_r']:+.2f}"
    )


def main() -> None:
    st.set_page_config(page_title="Portfolio Composer", page_icon="📦",
                        layout="wide")
    theme.inject_css()
    st.title("📦  Portfolio Composer")
    st.caption(
        "Pick a portfolio that hits your target trades/day. Sparse "
        "strategies (rare setups but high R:R + high win-rate) are "
        "weighted differently from frequent ones — risk is allocated "
        "from your daily-cap budget by data.")

    edges = _flatten_catalog()
    if not edges:
        st.warning("No optimizer data. Run `make optimize` first.")
        return

    # ── Composer config (moved from sidebar to main — Phase-32 UI) ───
    # Pre-fix this section lived in `st.sidebar` and crowded the page
    # nav with 13+ widgets. Now: nav stays clean, config sits inline
    # at top of main content, grouped into 3 expanders by purpose.
    # Sidebar is reserved for navigation + global account selector
    # only — same pattern as Backtest page.
    st.markdown("### 📦 Composer config")
    st.caption(
        "Defaults are the FTMO Challenge sweet spot. Twist any "
        "knob below to explore — values persist across reloads."
    )
    # The form code below uses an 'sb' indent context so we keep the
    # original control structure intact. We open a plain container
    # block instead of `st.sidebar`.
    if True:

        # ── 🎯 Filters (default expanded — primary controls) ──────────
        with st.expander("🎯  Filters", expanded=True):
            target_trades_day = float(st.slider(
                "Target trades/day", 1.0, 10.0, 3.0, 0.5,
                help="Combined trades/day across all selected strategies. "
                      "FTMO sweet spot is 1-5/day.",
            ))
            equity = float(st.number_input(
                "Account equity $", value=100_000.0,
                min_value=1_000.0, step=10_000.0,
            ))
            daily_cap_pct = float(st.number_input(
                "FTMO daily loss cap %", value=5.0,
                min_value=1.0, max_value=10.0, step=0.5,
            ))
            min_pass = float(st.slider("Min P(pass) %", 50, 100, 70, 5)) / 100.0
            min_n = int(st.slider("Min OOS trades", 10, 100, 15, 5))
            max_recov = int(st.slider(
                "Max recovery days", 30, 1000, 90, 10,
                help="Cells whose worst drawdown took longer than this "
                      "to recover are DROPPED. 90 = FTMO-safe.",
            ))
            max_strats = int(st.slider("Max strategies in portfolio",
                                           3, 20, 12, 1))
            trendo_only = st.checkbox(
                "✅  Trendo profitable only (EV per R > 0)",
                value=True,
                help="Math: WR × R:R − (1 − WR) > 0. Strategies that "
                      "don't satisfy this are mathematically losing.",
            )

        # ── 🎚 Risk mode (default expanded — affects sizing) ──────────
        with st.expander("🎚  Risk mode", expanded=True):
            risk_mode = st.radio(
                "How is your account doing?",
                ["recovery", "normal", "peak"],
                index=1, horizontal=True,
                captions=[
                    "in drawdown",
                    "near baseline",
                    "above baseline",
                ],
                help="Recovery: caps $/trade. Normal: standard "
                      "Trendo suggestion. Peak: multiplier × risk.",
            )
            if risk_mode == "recovery":
                current_equity_dd = float(st.number_input(
                    "Current account equity $",
                    value=90_000.0, min_value=1_000.0, step=1_000.0,
                ))
                max_loss_today = float(st.number_input(
                    "Max $ I can lose today",
                    value=1_000.0, min_value=50.0, step=100.0,
                ))
                min_trades_today_recovery = int(st.number_input(
                    "Min trades today",
                    value=10, min_value=1, max_value=50, step=1,
                ))
                peak_multiplier = 1.0
            elif risk_mode == "peak":
                current_equity_dd = float(st.number_input(
                    "Current account equity $",
                    value=110_000.0, min_value=1_000.0, step=1_000.0,
                ))
                peak_multiplier = float(st.slider(
                    "Peak risk multiplier", 1.0, 2.5, 1.5, 0.1,
                ))
                max_loss_today = 0.0
                min_trades_today_recovery = 1
            else:
                current_equity_dd = 100_000.0
                peak_multiplier = 1.0
                max_loss_today = 0.0
                min_trades_today_recovery = 1

        # ── 🔬 Stress-test (default collapsed — power-user knobs) ─────
        with st.expander("🔬  Stress-test auto-tune", expanded=False):
            st.caption(
                "When the worst-case streak drawdown would bust the "
                "FTMO floor, the auto-tuner scales every cell's risk "
                "DOWN by a single factor. Never scales up. Defaults "
                "are conservative — most users never need to adjust."
            )
            autotune_enabled = st.checkbox(
                "🛡️  Auto-shrink risk to fit FTMO floor",
                value=True,
            )
            ftmo_floor_pct = float(st.number_input(
                "FTMO total loss floor %",
                value=10.0, min_value=4.0, max_value=20.0, step=0.5,
            ))
            stress_target_pct = float(st.slider(
                "Stress-test target % of FTMO floor", 50, 95, 80, 5,
                help="80% = leave 20% safety margin below FTMO floor.",
            ) / 100.0)

        st.markdown("---")
        if st.button("🤖  Auto-optimize portfolio",
                       type="primary", width="stretch"):
            picked = _auto_optimize(
                edges,
                target_trades_per_day=target_trades_day,
                max_strategies=max_strats,
                min_pass_rate=min_pass,
                min_n_test=min_n,
                max_recovery_days=float(max_recov),
                trendo_zone_only=trendo_only,
            )
            st.session_state["pc_picked"] = [
                (e.strategy, e.ticker, e.tf, e.side) for e in picked
            ]
            st.toast(f"Picked {len(picked)} cells")

    # ── Selection priority order ──────────────────────────────────────
    #   1. Recommended preset (st.session_state["pc_force_picks"]) —
    #      takes precedence; user clicked "🌟 Load Recommended"
    #   2. Manual checkbox picks (st.session_state["pc_user_picks"]) —
    #      user unticked cells in the editable table
    #   3. Auto-optimize (default) — pc_picked from optimizer
    if "pc_force_picks" in st.session_state:
        st.session_state["pc_picked"] = st.session_state["pc_force_picks"]
    elif "pc_user_picks" in st.session_state:
        st.session_state["pc_picked"] = st.session_state["pc_user_picks"]
    elif "pc_picked" not in st.session_state:
        picked = _auto_optimize(
            edges,
            target_trades_per_day=target_trades_day,
            max_strategies=max_strats,
            min_pass_rate=min_pass,
            min_n_test=min_n,
            max_recovery_days=float(max_recov),
        )
        st.session_state["pc_picked"] = [
            (e.strategy, e.ticker, e.tf, e.side) for e in picked
        ]

    pick_keys = set(st.session_state["pc_picked"])
    selected = [e for e in edges
                 if (e.strategy, e.ticker, e.tf, e.side) in pick_keys]

    # ── Headline KPIs ─────────────────────────────────────────────────
    if not selected:
        st.warning("No cells qualified — relax filters in the sidebar.")
        return

    risk_alloc_raw = _suggest_risk_per_trade(selected,
                                                daily_cap_pct=daily_cap_pct)
    # Stress-test-aware auto-tune: if the worst-case streak drawdown
    # would breach the FTMO floor, scale every per-cell risk down by a
    # single multiplicative factor so worst case lands at
    # `stress_target_pct × ftmo_floor_pct` (default 80% × 10% = 8%).
    if autotune_enabled:
        risk_alloc_after_stress, autotune_k, _stress_after = (
            _autotune_risk_for_stress(
                selected, risk_alloc_raw,
                target_dd_pct=stress_target_pct * ftmo_floor_pct,
            )
        )
    else:
        risk_alloc_after_stress = risk_alloc_raw
        autotune_k = 1.0
    # Apply Risk-mode (Recovery / Normal / Peak) on top — caps or boosts
    # per-cell risk based on current account equity vs baseline.
    risk_alloc, risk_mode_info = _apply_risk_mode(
        selected, risk_alloc_after_stress,
        mode=risk_mode,
        current_equity=current_equity_dd,
        baseline_equity=100_000.0,
        max_loss_today_dollars=max_loss_today,
        min_trades_today=min_trades_today_recovery,
        peak_multiplier=peak_multiplier,
    )
    combined_tpd = sum(e.trades_per_day or 0 for e in selected)
    combined_tpm = sum(e.trades_per_month or 0 for e in selected)
    avg_pass = (sum(e.p_pass_30d or 0 for e in selected)
                / max(1, sum(1 for e in selected
                              if e.p_pass_30d is not None)))
    sum_net_pnl = sum(e.net_pnl_dollars for e in selected)
    avg_winrate = sum(e.win_rate_pct for e in selected) / len(selected)
    max_streak = max(e.max_consec_losses for e in selected)
    expected_daily_risk = sum(
        risk_alloc.get(e.strategy + "__" + e.ticker + "__" + e.tf, 0)
        * (e.trades_per_day or 0)
        * (1 - e.win_rate_pct / 100.0)
        for e in selected
    )

    cols = st.columns(6)
    cols[0].metric("Strategies", len(selected),
                      delta=f"target {target_trades_day:.1f}/day")
    cols[1].metric("Trades/day",
                      f"{combined_tpd:.2f}",
                      delta=f"{combined_tpm:.0f}/mo")
    cols[2].metric("Avg P(pass)", f"{avg_pass*100:.0f}%")
    cols[3].metric("Avg win%", f"{avg_winrate:.0f}%")
    cols[4].metric("Max consec losses",
                      max_streak, delta_color="inverse")
    cols[5].metric("Expected daily risk",
                      f"{expected_daily_risk:.2f}%",
                      delta=f"cap {daily_cap_pct:.1f}%",
                      delta_color="off")

    if expected_daily_risk > daily_cap_pct:
        st.error(
            f"⚠ Combined daily-loss exposure ({expected_daily_risk:.2f}%) "
            f"exceeds your FTMO cap ({daily_cap_pct:.1f}%). Reduce "
            f"strategy count or lower per-cell risk %."
        )
    elif combined_tpd < target_trades_day * 0.5:
        st.warning(
            f"⚠ Combined trades/day ({combined_tpd:.2f}) is well below "
            f"target ({target_trades_day:.1f}). Add higher-frequency "
            f"cells (M15) or relax the min P(pass) filter."
        )
    else:
        st.success(
            f"✅ Portfolio is in the FTMO sweet spot: "
            f"{combined_tpd:.2f} trades/day, {avg_pass*100:.0f}% avg "
            f"pass rate, {expected_daily_risk:.2f}% expected daily risk "
            f"(vs {daily_cap_pct:.1f}% cap)."
        )

    # ── Worst-case stress test ───────────────────────────────────────
    # Daily-risk number alone is misleading: it ignores losing streaks.
    # A 4.63%/day expected loss with L=4 streaks across 6 cells can blow
    # past the FTMO 10% floor in 2 days if all cells streak together. So
    # we model three scenarios and show the user what would actually
    # happen in each.

    # Banner when Risk-mode is active (Recovery / Peak)
    if risk_mode_info["applied"] and risk_mode == "recovery":
        st.warning(
            f"🟡 **Recovery mode** — account at "
            f"${risk_mode_info['current_equity']:,.0f} "
            f"({risk_mode_info['dd_pct']:+.1f}% vs $100k baseline). "
            f"Per-trade $ capped at "
            f"**${risk_mode_info['cap_dollars_per_trade']:,.0f}** "
            f"(= ${max_loss_today:,.0f} ÷ "
            f"{min_trades_today_recovery} trades), risk %% capped at "
            f"**{risk_mode_info['cap_risk_pct']:.3f}%**. Even if every "
            f"selected cell fires and stops out today, total loss stays "
            f"under your ${max_loss_today:,.0f} budget."
        )
    elif risk_mode_info["applied"] and risk_mode == "peak":
        st.success(
            f"🚀 **Peak mode** — account at "
            f"${risk_mode_info['current_equity']:,.0f}. Every cell's "
            f"risk multiplied by **{risk_mode_info['multiplier']:.1f}×** "
            f"to capture upside while in profit. Stress-test below shows "
            f"the post-multiplication exposure."
        )
    # Banner if the auto-tuner shrunk the suggested risks
    if autotune_enabled and autotune_k < 0.999:
        # Also compute pre-tune stress for the comparison
        _pre_metrics = _stress_test_metrics(selected, risk_alloc_raw)
        _post_metrics = _stress_test_metrics(selected, risk_alloc)
        st.info(
            f"🛡️ **Auto-tuned risks ↓ {(1-autotune_k)*100:.1f}%** to "
            f"keep worst-case under FTMO −{ftmo_floor_pct:.0f}%. "
            f"Independent stress drawdown went from "
            f"**−{_pre_metrics['indep_pct']:.2f}%** to "
            f"**−{_post_metrics['indep_pct']:.2f}%** "
            f"(target ≤ {stress_target_pct*ftmo_floor_pct:.1f}%). "
            f"Disable in the sidebar if you want the raw suggestion."
        )
    _render_stress_test(selected, risk_alloc, expected_daily_risk,
                          ftmo_floor_pct=ftmo_floor_pct)

    # ── Portfolio table ──────────────────────────────────────────────
    st.markdown("### Selected portfolio")

    # ── Recommended-preset shortcut ─────────────────────────────────
    # DYNAMIC preset (replaces the hard-coded 4-cell list that grew
    # stale every time we re-baselined). Reads the LIVE catalog at
    # click-time and picks the top-N deploy_safe cells (ones that
    # pass every hard gate). Diversifies across (strategy, ticker)
    # so the portfolio isn't all the same instrument.
    RECOMMENDED_TOP_N = 6
    # Build the recommended set from the SAME pool the editable table
    # uses (qualified_pool comes a few lines down — but we need it now,
    # so re-derive here from `edges`)
    _recommended_pool = [
        e for e in edges
        if e.deploy_safe
        and e.p_pass_30d is not None
        and e.p_pass_30d >= min_pass
        and e.n_test >= min_n
        and e.test_r > 0
        and (e.recovery_days is not None
              and e.recovery_days <= float(max_recov))
        and (not trendo_only or e.is_trendo_profitable)
    ]
    _recommended_pool.sort(key=lambda x: -x.score)
    # Diversify: at most one cell per (strategy, ticker) so we don't
    # end up with 6 EUR/USD variants. Walks down the score-sorted
    # list and keeps the first cell of each unique pair.
    _seen_pairs: set[tuple] = set()
    _recommended: list = []
    for e in _recommended_pool:
        pair = (e.strategy, e.ticker)
        if pair in _seen_pairs:
            continue
        _seen_pairs.add(pair)
        _recommended.append(e)
        if len(_recommended) >= RECOMMENDED_TOP_N:
            break

    if _recommended:
        with st.expander(
            f"🌟 Recommended pool ({len(_recommended)} top deploy-safe "
            f"cells, ranked by score) — preview before loading",
            expanded=False,
        ):
            for e in _recommended:
                rec = ('not yet' if e.recovery_days is None
                       else f'{e.recovery_days:.0f}d')
                st.markdown(
                    f"- **score {e.score:.1f}** · `{e.strategy}` × "
                    f"`{e.ticker}` × `{e.tf}` ({e.side}) · "
                    f"PF {e.test_pf:.2f} · R {e.test_r:+.2f} · "
                    f"recovery {rec} · n {e.n_test}"
                )
            st.caption(
                f"All cells pass: overall PF ≥ 1.05, TEST PF ≥ 1.00, "
                f"TEST avg_R ≥ 0, recovery ≤ 90d, n_test ≥ 15. "
                f"One cell per (strategy, ticker) for diversification."
            )
    else:
        st.warning(
            "🚫 No cells currently pass all deploy-safe gates. Either "
            "re-baseline (`python scripts/rebaseline_catalog.py`), or "
            "loosen the sidebar filters."
        )

    st.caption(
        "💡 **Tip:** click 'Load Recommended' to pre-tick these in the "
        "candidate pool below — you can then untick any you don't want, "
        "or scroll down to add others."
    )
    preset_cols = st.columns([2, 1, 4])
    if preset_cols[0].button(
        f"🌟  Load Recommended ({len(_recommended)} deploy-safe cells)",
        type="primary", width="stretch",
        key="pc_preset_elite",
        disabled=not _recommended,
        help="Picks the highest-scoring cells from the live catalog "
              "that pass EVERY hard gate (PF, OOS partition, recovery, "
              "sample size). Re-evaluated each click — re-baselining "
              "auto-updates this list, no code change needed.",
    ):
        st.session_state["pc_force_picks"] = [
            (e.strategy, e.ticker, e.tf, e.side) for e in _recommended
        ]
        st.toast(
            f"✅ Loaded {len(_recommended)} deploy-safe cells. Scroll "
            f"up to see the new stress test."
        )
        st.rerun()
    if preset_cols[1].button(
        "🔄  Reset to auto-pick", width="stretch",
        key="pc_preset_clear",
    ):
        st.session_state.pop("pc_force_picks", None)
        st.session_state.pop("pc_user_picks", None)
        st.toast("Auto-pick restored.")
        st.rerun()

    # ── Editable candidate pool ──────────────────────────────────────
    # Show ALL qualifying cells (not just the auto-picked ones) so the
    # user can ADD candidates as well as drop them. Auto-picked rows
    # come pre-ticked; everything else is unticked. User toggles → metrics
    # at the top of the page recompute on apply.
    #
    # HARD GATES applied here too so a "still underwater" or
    # "TEST PF<1" cell never even appears in the editable pool — the
    # user can't accidentally tick them. Cells that fail are surfaced
    # in a separate "Gated out" expander below for transparency.
    pool_pre_gate = [
        e for e in edges
        if e.p_pass_30d is not None
        and e.p_pass_30d >= min_pass
        and e.n_test >= min_n
        and e.test_r > 0
        and e.trades_per_day is not None
        and (e.recovery_days is not None
              and e.recovery_days <= float(max_recov))
        and (not trendo_only or e.is_trendo_profitable)
    ]
    qualified_pool = [e for e in pool_pre_gate if e.deploy_safe]
    gated_out = [e for e in pool_pre_gate if not e.deploy_safe]

    # ── Search / filter box ────────────────────────────────────────────
    # Free-text filter so the user doesn't have to scroll through 20+
    # candidates to find a specific one (e.g. "HK50 H1" or "donchian").
    # Matches against strategy + ticker + tf + side + R:R label, all
    # case-insensitive. Empty = show everything.
    search_cols = st.columns([3, 1])
    pool_search = search_cols[0].text_input(
        "🔎 Search candidates",
        value="",
        placeholder="e.g. 'HK50 H1' or 'donchian XAU' or 'M15 bidir'",
        key="pc_pool_search",
        help="Case-insensitive substring match across strategy, ticker, "
              "TF, side, and R:R label. Multiple terms are AND-ed.",
    )
    show_gated = search_cols[1].checkbox(
        "Show gate-failed too",
        value=False,
        key="pc_show_gated",
        help="Include cells that failed the hard gates — for research "
              "only. They're excluded from auto-pick + Recommended.",
    )

    # Build the searchable pool. When the user is actively searching,
    # BYPASS the sidebar filters (P(pass), n_test, recovery, trendo)
    # so a specific cell can always be found. Sidebar filters are for
    # narrowing the BROWSE view; search is for FINDING a known cell.
    # Without this bypass, a cell with p_pass=55% (below the default
    # 70% sidebar threshold) is invisible to search even if the user
    # types its exact name.
    if pool_search.strip():
        # Search mode: use ALL catalog cells, only gated by deploy_safe
        # (and show_gated toggle). Lets the user find any deploy-safe
        # cell regardless of sidebar P(pass)/n_test/recovery thresholds.
        # `edges` is already the flattened catalog (built in main() via
        # _flatten_catalog) — `cat` only exists inside that helper.
        if show_gated:
            searchable = list(edges)
        else:
            searchable = [e for e in edges if e.deploy_safe]
    else:
        # Browse mode: respect the sidebar-narrowed pool.
        searchable = list(qualified_pool)
        if show_gated:
            searchable.extend(gated_out)

    if pool_search.strip():
        # Tokenize loosely — strip separator chars users naturally type
        # when copy-pasting cell labels (×, ·, comma, parens). Each
        # remaining whitespace-separated term is required (AND).
        import re as _re
        cleaned = _re.sub(r"[×·,()]+", " ", pool_search)
        terms = [t.lower() for t in cleaned.split() if t.strip()]
        def _match(e) -> bool:
            haystack = (
                f"{e.strategy} {e.ticker} {e.tf} {e.side} "
                f"{e.rr_label or ''}"
            ).lower()
            return all(t in haystack for t in terms)
        searchable = [e for e in searchable if _match(e)]

    # Sort: currently-selected first (so user sees their picks at top),
    # then by score desc so high-quality candidates surface next.
    selected_keys = {(e.strategy, e.ticker, e.tf, e.side) for e in selected}
    searchable.sort(
        key=lambda e: (
            -1 if (e.strategy, e.ticker, e.tf, e.side) in selected_keys else 0,
            -e.score,
        )
    )
    # Display name `qualified_pool` keeps backward-compat with the rest
    # of the function body. The list it points to is now post-search.
    qualified_pool = searchable

    df_pool = _portfolio_table(qualified_pool, risk_alloc)
    df_pool.insert(
        0, "Keep",
        [
            (e.strategy, e.ticker, e.tf, e.side) in selected_keys
            for e in qualified_pool
        ],
    )
    # Backtest deep-link includes the FULL config_json (base64-encoded)
    # via the shared helper — every page that links to /Backtest goes
    # through dashboards.components.backtest_link.build_backtest_url
    # so a fresh run on Backtest reproduces the catalog metrics exactly.
    from dashboards.components.backtest_link import build_backtest_url
    df_pool["Backtest"] = [build_backtest_url(e) for e in qualified_pool]

    # Phase-32: Edge Score explainer — same component used on
    # Library / Compare / Backtest so the explanation is identical
    # everywhere.
    from dashboards.components import edge_score_explainer
    edge_score_explainer.render_explainer(expanded=False)

    # Caption — adapts to whether the user has typed a search query so
    # the displayed count is always meaningful.
    if pool_search.strip():
        st.caption(
            f"📋 Editable candidate pool — **{len(qualified_pool)}** "
            f"cells match `{pool_search}` "
            f"({'incl gate-failed' if show_gated else 'deploy-safe only'}). "
            f"**{len(selected)}** are currently in your portfolio (☑). "
            f"Tick MORE rows to add cells, untick to drop. Clear the "
            f"search to see the full pool."
        )
    else:
        st.caption(
            f"📋 Editable candidate pool — **{len(qualified_pool)} cells** "
            f"qualify (after sidebar filters + hard gates: P(pass) ≥ "
            f"{min_pass*100:.0f}%, recovery ≤ {max_recov}d, n_test ≥ "
            f"{min_n}, overall PF ≥ 1.05, TEST PF ≥ 1.00, TEST avg_R ≥ 0). "
            f"**{len(selected)}** are currently in your portfolio (☑). "
            f"Tick MORE rows to add cells, untick to drop. The metrics + "
            f"stress test at the top recompute on apply."
        )
    # Cost-config provenance badge — same model, same numbers, every page.
    st.caption(
        f"⚖️ **Benchmark** — {cost_defaults.cost_config_badge()}. "
        f"All candidate metrics produced under the SAME cost config "
        f"(also used by Backtest page, Library, Compare, sweep). "
        f"Re-baseline with `python scripts/rebaseline_catalog.py` "
        f"after any cost change."
    )

    # ── Gated-out cells (transparency) ────────────────────────────────
    # Show WHY each rejected cell didn't make the editable pool, so the
    # user understands the system's decisions instead of cells just
    # silently disappearing. Up to 30 reasons surfaced.
    if gated_out:
        with st.expander(
            f"🚫 {len(gated_out)} cells gated OUT (tap to see why)",
            expanded=False,
        ):
            st.caption(
                "These cells passed your sidebar filters but failed "
                "one or more hard gates — they're hidden from the "
                "editable pool to prevent accidental deployment. To "
                "include one anyway, lower the gate threshold in "
                "`core/edge_catalog.HARD_GATE_THRESHOLDS` and reload."
            )
            for e in sorted(gated_out, key=lambda x: -x.score)[:30]:
                reasons = " · ".join(e.hard_gate_failed)
                st.markdown(
                    f"- `{e.strategy}` × `{e.ticker}` × `{e.tf}` "
                    f"({e.side}) — **{reasons}**"
                )

    # Empty-pool guard. Pre-fix: a search query that matched 0 rows
    # produced a DataFrame whose "Keep" column had dtype float64 (numpy
    # default for an empty list of bools), and Streamlit's CheckboxColumn
    # rejects FLOAT data. The error surfaced at st.data_editor.
    if len(df_pool) == 0:
        if pool_search.strip():
            st.info(
                f"🔎 No cells match `{pool_search}`. Try a shorter "
                f"query (e.g. `HK50` instead of `ema_cross_12_26 HK50.cash H1 long, 1:2`), "
                f"tick **'Show gate-failed too'** to widen the search, or "
                f"clear the search to see the full pool."
            )
        else:
            st.info(
                "🔎 No deploy-safe candidates available. Try lowering "
                "`P(pass)` or `n_test` in the sidebar filters."
            )
        edited = df_pool   # empty placeholder; downstream change-detect is a no-op
    else:
        # Belt-and-suspenders: explicitly cast Keep to bool so even an
        # edge-case empty-after-filter scenario can't trip Streamlit's
        # FLOAT-dtype rejection.
        df_pool["Keep"] = df_pool["Keep"].astype(bool)
        edited = st.data_editor(
            df_pool,
            width="stretch",
            height=min(680, 38 * (len(df_pool) + 1)),
            column_config={
                "Keep": st.column_config.CheckboxColumn(
                    "Keep",
                    help="☑ = in your portfolio. Tick more or untick to "
                          "change the mix. Stress test recomputes on apply.",
                    default=False,
                ),
                "Backtest": st.column_config.LinkColumn(
                    "📊 Backtest",
                    display_text="Open",
                    help="Open this cell in the Backtest page for "
                          "equity curve / drawdown / trade tape.",
                ),
                "Suggested risk %": st.column_config.NumberColumn(
                    "Suggested risk %", format="%.3f%%",
                ),
                "Edge Score": st.column_config.NumberColumn(
                    "Edge Score", format="%.1f",
                    help=(
                        "Multi-metric ranking 0..100. Combines Kelly "
                        "+ Expectancy + Calmar + PSR + Sortino + "
                        "Sharpe-R + Recovery×Frequency + Deploy-safe. "
                        "Higher = more deployable. Open the explainer "
                        "expander for the full formula."
                    ),
                ),
                "Score (legacy)": st.column_config.NumberColumn(
                    "Score (legacy)", format="%.1f",
                    help="Legacy heuristic score (PF-biased). Kept "
                          "for comparison; prefer Edge Score for "
                          "production decisions.",
                ),
            },
            disabled=[c for c in df_pool.columns if c not in ("Keep",)],
            hide_index=True,
            key="pc_candidate_editor",
        )

    # Detect change vs current selection and offer Apply.
    # Search-aware: only the VISIBLE rows can be added/removed by the
    # current ticking. Cells hidden by the search filter are preserved
    # as-is (still in selected_keys → still in new_keys). Without this
    # guard, typing a search would falsely "remove" every previously-
    # selected cell that happens to be hidden by the query.
    visible_keys = {
        (qualified_pool[i].strategy, qualified_pool[i].ticker,
          qualified_pool[i].tf, qualified_pool[i].side)
        for i in range(len(qualified_pool))
    }
    visible_checked = {
        (qualified_pool[i].strategy, qualified_pool[i].ticker,
          qualified_pool[i].tf, qualified_pool[i].side)
        for i in range(len(qualified_pool))
        if bool(edited.iloc[i]["Keep"])
    }
    # New = (previously selected, now hidden) + (visible & checked)
    hidden_previously_selected = selected_keys - visible_keys
    new_keys = visible_checked | hidden_previously_selected
    if new_keys != selected_keys:
        added = new_keys - selected_keys
        removed = selected_keys - new_keys
        msg_parts = []
        if added:
            msg_parts.append(f"➕ {len(added)} added")
        if removed:
            msg_parts.append(f"➖ {len(removed)} removed")
        st.warning(
            f"📋 Pending change: {' · '.join(msg_parts)}. "
            f"Click below to apply (re-runs stress test + risk allocation).",
            icon="📋",
        )
        if st.button("✅ Apply selection",
                       type="primary", key="pc_apply_candidate_picks"):
            st.session_state["pc_user_picks"] = list(new_keys)
            st.session_state.pop("pc_force_picks", None)
            st.rerun()

    # ── Quick-action panel ────────────────────────────────────────────
    st.markdown("### ⚡  Quick actions per strategy")

    # ---- Bulk: Run parity on ALL selected cells ----
    bulk_cols = st.columns([2, 2, 4])
    if bulk_cols[0].button(f"🔬  Run parity on ALL ({len(selected)})",
                              type="primary",
                              width="stretch",
                              key="pc_act_parity_all",
                              help="Loops through every cell in the "
                                    "Selected portfolio and runs replay-"
                                    "parity for each. Required before "
                                    "promoting any cell to live."):
        from core.parity_gate import ParityGate
        from core.config import load_config
        from core.data import load_parquet
        from core.parity_check import run_parity
        from core.symbol_info_loader import try_load as _try_load
        from dashboards.components.state import (
            DEFAULT_LOTS, DEFAULT_MONEY_PER_UNIT, resolve_money_per_unit, discover_strategies as _ds,
        )
        import time
        gate = ParityGate(REPO / "data" / "v2.db")
        cfg = load_config()
        strategies = _ds()
        progress = st.progress(0.0, text="Running parity for selected cells…")
        results = []
        for i, edge in enumerate(selected):
            base = _resolve_base_strategy(edge.strategy, strategies)
            if base is None:
                results.append((edge, None, "no base found"))
                progress.progress((i + 1) / len(selected),
                                    text=f"[{i+1}/{len(selected)}] "
                                          f"{edge.strategy} × {edge.ticker} "
                                          f"— SKIP")
                continue
            StratCls, ParamsCls = strategies[base]
            try:
                strat = (StratCls() if ParamsCls is None
                          else StratCls(ParamsCls()))
                df = load_parquet(REPO / "data"
                                    / f"{edge.ticker}_{edge.tf}.parquet")
                sym_info = _try_load(edge.ticker)
                t0 = time.time()
                pr = run_parity(
                    df, strat, symbol=edge.ticker, tf=edge.tf,
                    starting_balance=100_000,
                    lots=DEFAULT_LOTS.get(edge.ticker, 0.1),
                    money_per_unit_price=DEFAULT_MONEY_PER_UNIT.get(
                        edge.ticker, 1.0),
                    commission_per_trade=3.0,
                    slippage_per_fill_atr_frac=0.1,
                    risk_cfg=cfg, symbol_info=sym_info,
                )
                div = pr.divergence_dollars
                if div < 0.01:
                    gate.record_pass(base, divergence_dollars=div)
                    results.append((edge, div, f"✅ ${div:.4f}"))
                else:
                    results.append((edge, div, f"⛔ ${div:.2f}"))
                progress.progress((i + 1) / len(selected),
                                    text=f"[{i+1}/{len(selected)}] "
                                          f"{edge.strategy} × {edge.ticker} "
                                          f"— ${div:.4f} ({time.time()-t0:.1f}s)")
            except Exception as e:
                results.append((edge, None,
                                  f"⛔ {type(e).__name__}: {e}"))
                progress.progress((i + 1) / len(selected),
                                    text=f"[{i+1}/{len(selected)}] "
                                          f"{edge.strategy} × {edge.ticker} "
                                          f"— ERROR")
        progress.empty()
        # Summary
        n_pass = sum(1 for _, d, _ in results if d is not None and d < 0.01)
        n_fail = sum(1 for _, d, _ in results if d is not None and d >= 0.01)
        n_skip = sum(1 for _, d, _ in results if d is None)
        st.success(
            f"✅ Bulk parity complete · {n_pass} passed · "
            f"{n_fail} failed · {n_skip} skipped (out of {len(selected)})"
        )
        with st.expander(f"Detailed results ({len(results)} cells)",
                            expanded=True):
            for edge, div, msg in results:
                st.markdown(
                    f"- **`{edge.strategy} × {edge.ticker} × {edge.tf}`** "
                    f"— {msg}"
                )
    bulk_cols[1].caption(
        "Or pick a single cell below for individual actions."
    )

    pick_options = [f"{e.strategy} · {e.ticker} · {e.tf} · {e.side}"
                     for e in selected]
    pick = st.selectbox(
        "Pick a row to act on", pick_options,
        key="pc_quick_pick",
        help="Run parity, backtest, or view full stats for any selected cell.",
    )
    pick_idx = pick_options.index(pick)
    pick_edge = selected[pick_idx]
    action_cols = st.columns(4)
    if action_cols[0].button("🔬  Run parity",
                                key="pc_act_parity",
                                width="stretch"):
        _run_parity_inline(pick_edge)
    if action_cols[1].button("📊  Run backtest",
                                key="pc_act_backtest",
                                width="stretch"):
        _run_backtest_inline(pick_edge)
    if action_cols[2].button("👁  View deep-dive",
                                key="pc_act_deepdive",
                                width="stretch",
                                help="Open this cell in Strategy Library "
                                      "with full equity curve, drawdown, "
                                      "trade tape."):
        st.session_state["lib_drilldown_pick"] = (
            f"{pick_edge.strategy} · {pick_edge.ticker} · {pick_edge.tf}"
        )
        st.toast("Switching to Strategy Library — drill-down pre-selected")
        try:
            st.switch_page("pages/7_🏛️_Strategy_Library.py")
        except Exception:
            st.info(
                "Open **🏛 Strategy Library** in the sidebar — your "
                f"row `{pick_edge.strategy} · {pick_edge.ticker} · "
                f"{pick_edge.tf}` is preselected for the deep-dive panel."
            )
    if action_cols[3].button("📦  Open in Backtest page",
                                key="pc_act_open_bt_page",
                                width="stretch"):
        # Preselect on the Backtest page so the user lands with the
        # correct ticker / tf / strategy already chosen in the sidebar
        # form, AND auto-run the backtest so they immediately see
        # results matching the cell they clicked.
        from dashboards.components.state import discover_strategies as _ds
        strategies = _ds()
        base = _resolve_base_strategy(pick_edge.strategy, strategies)
        if base is None:
            st.error(
                f"⚠ Cannot map `{pick_edge.strategy}` to a registered "
                f"strategy. Open Backtest page manually and pick "
                f"params yourself."
            )
        else:
            # Pre-fill all the form keys the Backtest page reads. Set
            # BEFORE switch_page so the destination page sees them on
            # first render.
            st.session_state["bt_ticker"] = pick_edge.ticker
            st.session_state["bt_tf"] = pick_edge.tf
            st.session_state["bt_strat"] = base
            # Auto-run flag — Backtest page reads this on load and
            # triggers the backtest with the preset values.
            st.session_state["_bt_auto_run"] = True
            st.session_state["_bt_auto_run_meta"] = {
                "ticker": pick_edge.ticker,
                "tf": pick_edge.tf,
                "strategy": base,
                "side": pick_edge.side,
                "rr_label": pick_edge.rr_label,
                "from_composer": True,
            }
            st.toast(
                f"Opening Backtest with `{base}` × {pick_edge.ticker} "
                f"× {pick_edge.tf} — auto-running"
            )
            try:
                st.switch_page("pages/1_📊_Backtest.py")
            except Exception:
                st.info(
                    "Switch to **📊 Backtest** in the sidebar — your "
                    "cell will auto-run on arrival."
                )

    # Render any pending action result inline
    if "_pc_action_result" in st.session_state:
        _render_action_result(st.session_state["_pc_action_result"])

    # ── Trade-frequency chart ────────────────────────────────────────
    st.markdown("### Trades-per-day breakdown")
    sorted_edges = sorted(selected, key=lambda e: e.trades_per_day or 0)
    labels = [f"{e.strategy}<br>{e.ticker} {e.tf}" for e in sorted_edges]
    tpds = [e.trades_per_day or 0 for e in sorted_edges]
    colors = ["#dc2626" if (e.p_pass_30d or 0) < 0.5
               else "#fbbf24" if (e.p_pass_30d or 0) < 0.85
               else "#16a34a" for e in sorted_edges]
    fig = go.Figure(go.Bar(
        x=tpds, y=labels, orientation="h",
        marker_color=colors,
        text=[f"{v:.2f}/day  ({(e.trades_per_month or 0):.0f}/mo)"
              for v, e in zip(tpds, sorted_edges)],
        textposition="outside",
    ))
    fig.add_vline(x=target_trades_day, line=dict(color="#9ca3af", dash="dash"),
                   annotation_text=f"target {target_trades_day:.1f}/day combined",
                   annotation_position="top right")
    fig.update_layout(
        height=max(280, 36 * len(selected) + 80),
        margin=dict(l=10, r=10, t=40, b=10),
        title="Per-strategy trades/day  (red=low pass, yellow=med, green=high)",
        plot_bgcolor="#0b1117", paper_bgcolor="#0b1117",
        font=dict(color="#cbd5e1"),
        xaxis=dict(gridcolor="#1f2937", title="trades/day"),
        yaxis=dict(gridcolor="#1f2937"),
    )
    st.plotly_chart(fig, width="stretch")

    # ── Deploy ───────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 🚀  Deploy to active account")
    try:
        accounts = account_manager.list_accounts()
        active = accounts[0] if accounts else None
    except Exception:
        active = None
    if active is None:
        st.warning("No MT5 account configured.")
        return

    cols = st.columns([3, 1, 1])
    cols[0].markdown(
        f"Account **{active.alias}** (#{active.login}) — equity "
        f"${active.effective_baseline_equity:,.0f}.  Each strategy "
        f"will be deployed with its **suggested risk %** from the table.")
    deploy_status = cols[1].radio(
        "as", ["paper", "live"], horizontal=True,
        key="pc_deploy_status",
    )
    if cols[2].button("🚀 Review & Deploy",
                        type="primary", width="stretch",
                        key="pc_deploy_btn"):
        # ONE-SHOT trigger pattern: set _pc_deploy_pending to True
        # on the click, then consume + clear it on the next render.
        # Without this, the old _pc_deploy_dialog_open flag stayed
        # True after the user dismissed via the X button, and EVERY
        # subsequent rerun (e.g. changing any sidebar slider) would
        # re-open the dialog. Now: each Review-click triggers exactly
        # one dialog render.
        st.session_state["_pc_deploy_pending"] = True
        st.session_state["_pc_deploy_target"] = deploy_status
        st.session_state["_pc_deploy_selected_keys"] = [
            (e.strategy, e.ticker, e.tf, e.side) for e in selected
        ]

    # Confirmation dialog — opens ONCE per Review-click. Consume the
    # pending flag immediately so reruns triggered by inputs INSIDE the
    # dialog (or anywhere else on the page) don't re-trigger it.
    if st.session_state.pop("_pc_deploy_pending", False):
        _render_deploy_dialog(active, selected, risk_alloc,
                                daily_cap_pct=daily_cap_pct,
                                target_status=st.session_state.get(
                                    "_pc_deploy_target", "paper"))


@st.dialog("Confirm portfolio deploy", width="large")
def _render_deploy_dialog(account, selected, risk_alloc, *,
                            daily_cap_pct, target_status):
    """Modal with editable risk/cap per row + parity check + Confirm/Cancel."""
    gate = ParityGate(DB_PATH)
    equity = float(account.effective_baseline_equity)

    st.markdown(
        f"Deploying **{len(selected)}** strategies to **{account.alias}** "
        f"(#{account.login}) as **{target_status.upper()}**. Tweak risk % "
        f"or daily cap % per row before confirming."
    )

    # ── Collision check: scan existing deployments on this account ────
    # Existing deployments may share the same (strategy, ticker, tf) with
    # a different deployment_id (e.g. legacy slug without `_long` suffix
    # vs Composer's `_long`/`_bidir` suffix). If we just upsert, those
    # would coexist — both signal sources firing. Surface that here.
    existing_deps = dep_mod.load_deployments(account.login)
    existing_by_id = {d.deployment_id: d for d in existing_deps}
    existing_by_key = {}
    for d in existing_deps:
        key = (d.strategy, d.ticker, d.tf, d.long_only)
        existing_by_key.setdefault(key, []).append(d)

    new_count = 0
    update_count = 0
    collision_count = 0
    collision_rows = []
    for e in selected:
        long_only = (e.side == "long")
        new_dep_id = (dep_mod.Deployment.slug(e.strategy, e.ticker, e.tf)
                       + ("_long" if long_only else "_bidir"))
        key = (e.strategy, e.ticker, e.tf, long_only)
        if new_dep_id in existing_by_id:
            update_count += 1
        else:
            similar = existing_by_key.get(key, [])
            if similar:
                collision_count += 1
                collision_rows.append((e, similar))
            else:
                new_count += 1

    if collision_count > 0 or update_count > 0:
        with st.container(border=True):
            st.markdown(
                f"#### 🔍 Existing deployments on this account "
                f"({len(existing_deps)} total)"
            )
            sum_cols = st.columns(3)
            sum_cols[0].metric(
                "🆕 New", new_count,
                help="No existing deployment with the same id — will be "
                      "added.")
            sum_cols[1].metric(
                "🔁 Update", update_count,
                help="An existing deployment with the same id will be "
                      "OVERWRITTEN with the new risk %, daily cap %, and "
                      "status. Status changes from paused/halted → "
                      "paper/live.")
            sum_cols[2].metric(
                "⚠ Collide", collision_count,
                delta="duplicate signal source",
                delta_color="inverse",
                help="An existing deployment trades the same "
                      "(strategy, ticker, tf, side) with a different id "
                      "(legacy slug). Composer will ADD this as a "
                      "second deployment — both will fire on the same "
                      "signals, doubling exposure. Resolve by removing "
                      "or pausing the legacy deployment from the "
                      "Operations page first.")
            if collision_count > 0:
                st.error(
                    f"⛔ **{collision_count} collision(s)** — a legacy "
                    f"deployment trades the same setup. Click an item "
                    f"below to see the existing id and decide whether "
                    f"to remove it before deploying."
                )
                for e, similars in collision_rows:
                    with st.expander(
                        f"⚠ `{e.strategy}` · `{e.ticker}` `{e.tf}` "
                        f"({e.side}) — collides with "
                        f"{len(similars)} existing",
                    ):
                        for s in similars:
                            st.markdown(
                                f"- existing id `{s.deployment_id}` "
                                f"(status **{s.status}**, risk "
                                f"{s.risk_pct:.2f}%, daily cap "
                                f"{s.daily_cap_pct:.2f}%)"
                            )
                        st.caption(
                            "💡 Remove these from the Operations page "
                            "(or pause them) so the Composer's new "
                            "deployment is the single source of truth, "
                            "or skip this row when confirming below."
                        )
            elif update_count > 0:
                st.info(
                    f"🔁 {update_count} row(s) will overwrite an "
                    f"existing deployment with the same id (same "
                    f"strategy/ticker/tf/side). Their risk %, daily "
                    f"cap %, and status will be replaced."
                )

    # Editable rows — track edits in session state per cell
    edits: dict[str, dict] = {}
    parity_blocked: list[str] = []
    # Resolve every variant to its registered base class once so the
    # parity check uses the correct key (parity_log stores base names
    # like 'ema_cross', not variants like 'ema_cross_9_20').
    from dashboards.components.state import discover_strategies as _ds
    _registered = _ds()
    for e in selected:
        slug = e.strategy + "__" + e.ticker + "__" + e.tf
        suggested_risk = risk_alloc.get(slug, 0.30)
        per_strat_cap = daily_cap_pct / max(1, len(selected))
        # Map variant → base for parity lookup
        base_name = (_resolve_base_strategy(e.strategy, _registered)
                      or e.strategy)

        with st.container(border=True):
            head = st.columns([3, 1, 1, 1])
            parity_ok = gate.is_recent(base_name)
            last = gate.last_pass_for(base_name)
            if parity_ok:
                ts = last[0].strftime("%Y-%m-%d %H:%M") if last else ""
                parity_chip = (
                    f"<span style='color:#16a34a;font-weight:600;'>"
                    f"✅ parity ok ({base_name}, {ts} UTC)</span>"
                )
            else:
                row_action = st.button(
                    "🔬 Run parity now",
                    key=f"_dlg_parity_{slug}",
                    type="primary",
                )
                if row_action:
                    _run_parity_for_dialog(e, base_name, gate)
                    st.rerun()
                parity_chip = (
                    f"<span style='color:#dc2626;font-weight:600;'>"
                    f"⚠ no parity for `{base_name}` → will fall back "
                    f"to PAPER (click button to run inline)</span>"
                )
            if target_status == "live" and not parity_ok:
                parity_blocked.append(base_name)

            # Build deep-link to Backtest page so user can inspect
            # this cell before committing to deploy. Includes the full
            # source_config_json so the fresh run matches the catalog.
            from dashboards.components.backtest_link import (
                build_backtest_url as _bt_url,
            )
            backtest_url = _bt_url(e, relative=True)
            head[0].markdown(
                f"**`{e.strategy}`** · `{e.ticker}` `{e.tf}` "
                f"({e.side}) · R:R **{e.rr_label or '—'}**  "
                f"&nbsp; {parity_chip}  "
                f"&nbsp; <a href='{backtest_url}' target='_self' "
                f"style='font-size:0.85rem;color:#60a5fa;'>📊 backtest</a>",
                unsafe_allow_html=True,
            )
            new_risk = float(head[1].number_input(
                "risk %", value=float(suggested_risk),
                min_value=0.05, max_value=2.0, step=0.05,
                format="%.2f", key=f"_dlg_risk_{slug}",
            ))
            new_cap = float(head[2].number_input(
                "daily cap %", value=float(per_strat_cap),
                min_value=0.1, max_value=10.0, step=0.1,
                format="%.2f", key=f"_dlg_cap_{slug}",
            ))
            risk_dollars = equity * new_risk / 100.0
            head[3].markdown(
                f"<div style='font-family:ui-monospace,Menlo,monospace;"
                f"font-size:0.78rem;line-height:1.5;'>"
                f"<span style='color:#9ca3af;'>$/trade</span> "
                f"<b>${risk_dollars:,.0f}</b><br>"
                f"<span style='color:#9ca3af;'>~{e.trades_per_day or 0:.2f}/day</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
            # ❌ Remove from deploy list — drops this cell from
            # pc_picked AND closes the dialog so the user lands
            # back on the Composer page with the new selection.
            if st.button("❌ Remove from deploy",
                            key=f"_dlg_remove_{slug}",
                            help="Drop this strategy from the "
                                  "deploy. The Composer's portfolio "
                                  "table updates and the stress test "
                                  "recomputes — re-click Deploy when "
                                  "ready."):
                # Update both the live picker and the user-picks
                # cache so the Composer reflects the change after
                # the dialog closes.
                cur = list(st.session_state.get("pc_picked", []))
                want = (e.strategy, e.ticker, e.tf, e.side)
                cur = [x for x in cur if x != want]
                st.session_state["pc_picked"] = cur
                st.session_state["pc_user_picks"] = cur
                st.session_state.pop("pc_force_picks", None)
                st.session_state.pop("_pc_deploy_pending", None)
                st.toast(
                    f"❌ Removed {e.strategy} × {e.ticker} {e.tf} "
                    f"({e.side}) from deploy. "
                    f"Re-click Deploy when ready."
                )
                st.rerun()
            edits[slug] = {
                "risk_pct": new_risk,
                "daily_cap_pct": new_cap,
                "edge": e,
            }

    # Aggregate preview
    total_daily_risk = sum(
        edits[s]["risk_pct"] * (edits[s]["edge"].trades_per_day or 0)
        * (1 - edits[s]["edge"].win_rate_pct / 100.0)
        for s in edits
    )
    sum_cols = st.columns(3)
    sum_cols[0].metric("Total strategies", len(selected))
    sum_cols[1].metric("Aggregate per-trade risk",
                          f"{sum(e['risk_pct'] for e in edits.values()):.2f}%",
                          help="Sum of risk%/trade if every cell fired same bar")
    sum_cols[2].metric("Expected daily-loss exposure",
                          f"{total_daily_risk:.2f}%",
                          delta=f"vs {daily_cap_pct:.1f}% cap",
                          delta_color=("normal" if total_daily_risk
                                         <= daily_cap_pct else "inverse"))

    if total_daily_risk > daily_cap_pct:
        st.error(
            f"⚠ Combined daily-loss exposure ({total_daily_risk:.2f}%) "
            f"exceeds your FTMO cap ({daily_cap_pct:.1f}%). Lower some "
            f"risk values before confirming."
        )

    if parity_blocked and target_status == "live":
        st.warning(
            f"⚠ {len(parity_blocked)} strateg(ies) will fall back to "
            f"**paper** because they don't have a recent replay-parity "
            f"pass: " + ", ".join(f"`{s}`" for s in parity_blocked)
        )

    # ── Quality gate scan ────────────────────────────────────────
    # Refuse to deploy ANY row that fails the quality gate. We show
    # the per-row verdict here and remove blocked rows from the deploy
    # batch. Override available below for the whole batch.
    from core.deployment_quality_gate import (
        evaluate_quality as _eval_quality,
        load_criteria as _load_q_criteria,
        QualityCriteria as _QC,
    )
    _q_criteria = (_load_q_criteria(account.login)
                       if target_status == "live" else _QC.lenient())
    quality_results: dict[str, "object"] = {}
    blocked_slugs: list[str] = []
    warned_slugs: list[str] = []
    for slug, cfg_row in edits.items():
        qres = _eval_quality(cfg_row["edge"], criteria=_q_criteria)
        quality_results[slug] = qres
        if qres.is_blocked:
            blocked_slugs.append(slug)
        elif qres.verdict == "WARN":
            warned_slugs.append(slug)

    if blocked_slugs:
        with st.container(border=True):
            st.error(
                f"⛔ **Quality gate: {len(blocked_slugs)} of "
                f"{len(edits)} strategies BLOCKED**"
            )
            for slug in blocked_slugs:
                e = edits[slug]["edge"]
                qres = quality_results[slug]
                with st.expander(
                    f"⛔ `{e.strategy}` × `{e.ticker}` `{e.tf}` "
                    f"({e.side}) — {len(qres.block_reasons)} failure(s)"
                ):
                    for reason in qres.block_reasons:
                        st.markdown(f"- {reason}")
            st.markdown(
                "These rows will be **excluded** from the deploy. To "
                "override (force paper-only) type "
                "`I UNDERSTAND` below."
            )
    if warned_slugs:
        st.warning(
            f"🟡 {len(warned_slugs)} of {len(edits)} strategies have "
            f"quality WARN concerns — they will deploy but you should "
            f"review (expand each row above for details)."
        )

    quality_override_token = ""
    if blocked_slugs:
        quality_override_token = st.text_input(
            "Quality override (`I UNDERSTAND` — force-deploys "
            "blocked rows as paper)",
            value="", key="_pc_quality_override",
            placeholder="leave blank to skip blocked rows",
        )

    # Action buttons
    btn_cols = st.columns([1, 1, 2])
    # Deploy disabled if combined daily risk too high OR every row is
    # blocked (nothing to deploy)
    nothing_to_deploy = (len(blocked_slugs) == len(edits)
                            and quality_override_token != "I UNDERSTAND")
    deploy_disabled = (total_daily_risk > daily_cap_pct
                          or nothing_to_deploy)
    if btn_cols[0].button("✅ Confirm deploy", type="primary",
                            width="stretch",
                            disabled=deploy_disabled):
        n_done = 0
        n_fallback = 0
        n_q_blocked = 0
        n_q_overridden = 0
        for slug, cfg in edits.items():
            e = cfg["edge"]
            qres = quality_results[slug]
            # If quality blocked AND no override → skip
            if qres.is_blocked and quality_override_token != "I UNDERSTAND":
                n_q_blocked += 1
                continue
            base = (_resolve_base_strategy(e.strategy, _registered)
                     or e.strategy)
            actual_status = target_status
            # Override forces paper for safety
            if qres.is_blocked and quality_override_token == "I UNDERSTAND":
                actual_status = "paper"
                n_q_overridden += 1
            elif target_status == "live" and not gate.is_recent(base):
                actual_status = "paper"
                n_fallback += 1
            dep_id = (dep_mod.Deployment.slug(e.strategy, e.ticker, e.tf)
                      + ("_long" if e.side == "long" else "_bidir"))
            # Hard $-cap per trade: this deployment's slice of the daily
            # FTMO budget, divided by its expected trades/day. Floor at $50
            # so a trivially-tiny number doesn't make the cap impossible.
            # If the strategy fires more than expected, the cap protects;
            # if it fires less, no harm. Worst case: 1 trade only sized
            # to risk this much.
            cap_dollars = (equity * cfg["daily_cap_pct"] / 100.0)
            tpd = max(1.0, float(e.trades_per_day or 1.0))
            max_money_risk_usd = max(50.0, cap_dollars / tpd)
            d = dep_mod.Deployment(
                deployment_id=dep_id,
                strategy=e.strategy, ticker=e.ticker, tf=e.tf,
                long_only=(e.side == "long"),
                params={"long_only": (e.side == "long")},
                risk_pct=cfg["risk_pct"],
                daily_cap_pct=cfg["daily_cap_pct"],
                max_money_risk_usd=round(max_money_risk_usd, 2),
                status=actual_status,
                notes=(f"Composer: {e.trades_per_day or 0:.2f}/day, "
                        f"P(pass) {(e.p_pass_30d or 0)*100:.0f}%, "
                        f"R:R {e.rr_label}, "
                        f"max-$ ${max_money_risk_usd:.0f}/trade"
                        + (" [QUALITY-OVERRIDE]"
                            if qres.is_blocked
                            and quality_override_token == "I UNDERSTAND"
                            else "")),
            )
            dep_mod.upsert_deployment(account.login, d)
            n_done += 1
        # No flag to clear — _pc_deploy_pending is consumed by .pop()
        # at the call site, so the dialog is one-shot by construction.
        msg = f"✅ Deployed {n_done} strategies as `{target_status}`."
        if n_fallback:
            msg += f"  ⚠ {n_fallback} fell back to paper (no parity)."
        if n_q_blocked:
            msg += (f"  ⛔ {n_q_blocked} skipped by quality gate "
                    f"(see expanders above).")
        if n_q_overridden:
            msg += (f"  ⚠ {n_q_overridden} quality-overridden, "
                    f"forced to paper.")
        st.success(msg)
        st.toast("Portfolio deployed.")
        st.rerun()
    if btn_cols[1].button("Cancel", width="stretch"):
        # Dialog is one-shot — just rerun to close it.
        st.rerun()
    btn_cols[2].caption(
        "Tip: edits made in this dialog are applied as overrides — your "
        "original risk allocation table on the page is untouched."
    )


main()
