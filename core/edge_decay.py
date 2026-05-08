"""
edge_decay.py — auto-detect when a deployment's live edge has drifted
materially below its catalog expectation.

Why this exists
---------------
Edge depletes over time. A cell that backtested at PF 1.9 will, sooner
or later, experience regime change, structural break, or just statistical
mean-reversion of its outlier-good test slice. Catching that drift before
it costs money is more valuable than finding any new strategy.

Mechanism (defensible quant style)
----------------------------------
For every closed trade in a live or paper deployment, compute live_R
(realized PnL / initial dollar risk). Maintain a rolling window of the
last `WINDOW_TRADES` (default 30) live_R values per deployment. Compare
the rolling mean to the catalog's expectancy_per_R via a z-score:

    z = (mean(live_R) - catalog_R) / (sigma(live_R) / sqrt(n))

This is a one-sample t-test against the catalog null hypothesis. The
classification is:

    z > +1                          → BLUE   (over-performing — could size up)
    -1 ≤ z ≤ +1                     → GREEN  (within statistical noise — keep running)
    -2 ≤ z < -1                     → YELLOW (underperforming — investigate)
    z < -2 sustained for ≥ MIN_RED_TRADES (15) → RED (auto-demote live → paper)

The RED state requires sustained underperformance, not a single bad
window, to avoid false demotions on noise. We require the last
MIN_RED_TRADES windows to all have z < -2 before flipping.

Why z-score and not Sharpe drift
--------------------------------
Sharpe drift is unitless and robust but doesn't tell you "how surprised
should I be at this drawdown vs the catalog's published edge?" The
z-score answers that question directly: z = -2 means "the current
performance is 2 standard errors below catalog — probability under the
catalog's null is ~2.5%." That's a defensible auto-demote threshold
(95% confidence).

Storage
-------
Per-deployment rolling window persisted in v2.db `edge_decay_samples`
(append-only). Aggregated assessment computed on demand from latest
N rows — no separate state table.

Public API
----------
- record_trade(db_path, deployment_id, live_R, catalog_R, closed_at_utc)
- assess(db_path, deployment_id, catalog_R) → DecayAssessment
- assess_all(db_path) → list[DecayAssessment]  (for Command Center tile)
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------- #
# Tunable thresholds — single source of truth                      #
# ---------------------------------------------------------------- #
WINDOW_TRADES = 30          # rolling-window size
MIN_TRADES_TO_ASSESS = 8    # below this, assessment is GREEN regardless
MIN_RED_TRADES = 15         # consecutive z<-2 trades to trigger RED auto-demote
Z_BLUE = 1.0
Z_YELLOW = -1.0
Z_RED = -2.0


class DecayState(str, Enum):
    """Per-deployment edge-decay classification."""
    BLUE = "blue"        # over-performing
    GREEN = "green"      # within noise
    YELLOW = "yellow"    # underperforming, watch
    RED = "red"          # sustained underperformance, auto-demote
    INSUFFICIENT = "insufficient"  # not enough trades to assess


@dataclass
class DecayAssessment:
    """Result of assessing one deployment's edge-decay status."""
    deployment_id: str
    state: DecayState
    n_trades: int
    z_score: Optional[float]
    rolling_mean_R: Optional[float]
    catalog_R: float
    sustained_red_trades: int  # count of consecutive z<-2 trades from latest
    reason: str

    @property
    def should_demote(self) -> bool:
        return self.state == DecayState.RED


# ---------------------------------------------------------------- #
# Schema                                                           #
# ---------------------------------------------------------------- #
_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS edge_decay_samples (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        closed_at_utc       TEXT    NOT NULL,
        deployment_id       TEXT    NOT NULL,
        live_R              REAL    NOT NULL,
        catalog_R           REAL    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_edge_decay_dep
        ON edge_decay_samples(deployment_id, closed_at_utc DESC);
"""


def _ensure_schema(db_path: Path) -> None:
    """Create the table + index if not present. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA_DDL)
        conn.commit()


# ---------------------------------------------------------------- #
# Recording                                                        #
# ---------------------------------------------------------------- #
def record_trade(db_path: Path, *, deployment_id: str,
                   live_R: float, catalog_R: float,
                   closed_at_utc: Optional[str] = None) -> None:
    """Append one closed-trade R-multiple to the decay history.

    Called from DeploymentRunner on every trade close (paper + live).
    Both branches are tracked because paper feed-back informs us whether
    the cell is decaying even before live capital is at risk.
    """
    _ensure_schema(db_path)
    if closed_at_utc is None:
        closed_at_utc = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO edge_decay_samples
                (closed_at_utc, deployment_id, live_R, catalog_R)
            VALUES (?, ?, ?, ?)
            """,
            (closed_at_utc, deployment_id, float(live_R), float(catalog_R)),
        )
        conn.commit()


def _fetch_recent_R(db_path: Path, deployment_id: str,
                       limit: int) -> list[float]:
    """Return the most recent `limit` live_R values for this deployment,
    newest-first. Empty list if the table doesn't exist yet."""
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT live_R FROM edge_decay_samples
                 WHERE deployment_id = ?
                 ORDER BY closed_at_utc DESC, id DESC
                 LIMIT ?
                """,
                (deployment_id, int(limit)),
            ).fetchall()
        return [float(r[0]) for r in rows]
    except sqlite3.OperationalError:
        # Table missing — first call, no data yet
        return []


# ---------------------------------------------------------------- #
# Statistics — pure functions, easy to test                        #
# ---------------------------------------------------------------- #
def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    """Sample standard deviation (n-1 denominator). Returns 0 if <2."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _z_score(live_mean: float, catalog_R: float, sample_stdev: float,
                n: int) -> Optional[float]:
    """One-sample t-test z-score: how many standard errors below catalog?
    Returns None when stderr is zero (degenerate sample)."""
    if n < 2 or sample_stdev <= 0:
        return None
    stderr = sample_stdev / math.sqrt(n)
    if stderr <= 0:
        return None
    return (live_mean - catalog_R) / stderr


def _classify(z: Optional[float], n_trades: int,
                 sustained_red: int) -> DecayState:
    """Map z-score + sustained-red count to a state."""
    if n_trades < MIN_TRADES_TO_ASSESS:
        return DecayState.INSUFFICIENT
    if z is None:
        return DecayState.GREEN
    if z >= Z_BLUE:
        return DecayState.BLUE
    if z <= Z_RED and sustained_red >= MIN_RED_TRADES:
        return DecayState.RED
    if z <= Z_YELLOW:
        return DecayState.YELLOW
    return DecayState.GREEN


def _sustained_red_count(rs: list[float], catalog_R: float) -> int:
    """Count consecutive trades (newest-first) whose individual contribution
    to the rolling mean falls below the z<-2 threshold relative to catalog.
    Approximation: each trade's contribution is just (R - catalog_R); we
    track how many of the LATEST trades had R materially below catalog."""
    if len(rs) < 2:
        return 0
    sigma = _stdev(rs)
    if sigma <= 0:
        return 0
    # A trade is "red-contributing" if its R is more than 2σ below catalog.
    # rs is newest-first; count from the head until we hit a non-red trade.
    threshold = catalog_R - 2.0 * sigma
    count = 0
    for r in rs:
        if r < threshold:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------- #
# Public assessment API                                            #
# ---------------------------------------------------------------- #
def assess(db_path: Path, *, deployment_id: str,
              catalog_R: float) -> DecayAssessment:
    """Assess one deployment's edge-decay state.

    Returns DecayAssessment with state, z-score, rolling stats, and a
    human-readable reason. Caller (DeploymentRunner / Command Center)
    uses .should_demote to decide whether to flip live → paper.
    """
    rs = _fetch_recent_R(db_path, deployment_id, WINDOW_TRADES)
    n = len(rs)
    if n == 0:
        return DecayAssessment(
            deployment_id=deployment_id,
            state=DecayState.INSUFFICIENT,
            n_trades=0,
            z_score=None,
            rolling_mean_R=None,
            catalog_R=float(catalog_R),
            sustained_red_trades=0,
            reason="no closed trades yet",
        )
    live_mean = _mean(rs)
    sigma = _stdev(rs)
    z = _z_score(live_mean, catalog_R, sigma, n)
    sustained_red = _sustained_red_count(rs, catalog_R)
    state = _classify(z, n, sustained_red)

    if state == DecayState.RED:
        reason = (
            f"RED — z={z:.2f}, last {sustained_red} trades all "
            f">2σ below catalog; auto-demote recommended"
        )
    elif state == DecayState.YELLOW:
        reason = (
            f"YELLOW — z={z:.2f}, live mean R={live_mean:+.3f} vs "
            f"catalog {catalog_R:+.3f}; investigate"
        )
    elif state == DecayState.BLUE:
        reason = (
            f"BLUE — z={z:.2f}, live mean R={live_mean:+.3f} > "
            f"catalog {catalog_R:+.3f}; over-performing"
        )
    elif state == DecayState.INSUFFICIENT:
        reason = (
            f"INSUFFICIENT — only {n} closed trades, need "
            f"{MIN_TRADES_TO_ASSESS} for assessment"
        )
    else:
        reason = (
            f"GREEN — z={(z if z is not None else 0):.2f}, live "
            f"R={live_mean:+.3f} within noise of catalog "
            f"{catalog_R:+.3f}"
        )

    return DecayAssessment(
        deployment_id=deployment_id,
        state=state,
        n_trades=n,
        z_score=z,
        rolling_mean_R=live_mean,
        catalog_R=float(catalog_R),
        sustained_red_trades=sustained_red,
        reason=reason,
    )


def assess_all(db_path: Path,
                  catalog_R_lookup: dict[str, float]) -> list[DecayAssessment]:
    """Assess every deployment that has at least one recorded trade.

    `catalog_R_lookup` maps deployment_id → its catalog expectancy_per_R,
    looked up by the caller from edge_catalog. Deployments not in the
    lookup are skipped (we don't fabricate a catalog R).
    """
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT deployment_id
                  FROM edge_decay_samples
                """
            ).fetchall()
    except sqlite3.OperationalError:
        return []

    assessments: list[DecayAssessment] = []
    for (dep_id,) in rows:
        if dep_id not in catalog_R_lookup:
            continue
        assessments.append(
            assess(db_path,
                     deployment_id=dep_id,
                     catalog_R=catalog_R_lookup[dep_id])
        )
    return assessments
