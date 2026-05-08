"""
correlation_clusters.py — cap concurrent positions across correlated cells.

`position_guard.py` blocks colliding positions on the SAME symbol
(strict policy). That handles the case where 3 XAGUSD cells from 3
strategies try to open at once — only one wins.

What it doesn't handle: 4 different metals (XAUUSD, XAGUSD, XPTUSD,
XPDUSD) opening at the same time. Each is on a different symbol so
the strict guard allows them, but they're all the same correlated
risk factor (USD-denominated precious metals — when the dollar runs,
they ALL go together). On a $100k FTMO with 0.05% per cell, that's
0.20% directional dollar risk, exactly the concentration we want to
avoid.

This module sits ABOVE position_guard. It groups cells into named
"correlation clusters" and caps how many cells per cluster can hold a
position simultaneously.

Example:
    clusters = default_clusters()
    # → {"metals_long": ["XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"],
    #    "us_indices":   ["US100.cash", "US500.cash", "US30.cash"], ...}

    decision = check_cluster_open(
        symbol="XAUUSD",
        open_positions=current_open,
        clusters=clusters,
        max_concurrent_per_cluster=2,
    )
    # decision.decision is "ALLOW" or "BLOCK"

Wiring: call this BEFORE position_guard.check_open in the runner. If
either guard rejects, skip the open.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

from core.position_guard import OpenPosition


ClusterDecision = Literal["ALLOW", "BLOCK"]


@dataclass(frozen=True)
class ClusterResult:
    decision: ClusterDecision
    cluster_name: str | None
    current_count: int
    cap: int
    reason: str


# ---------------------------------------------------------------------------
# Cluster definitions
# ---------------------------------------------------------------------------

def default_clusters() -> dict[str, list[str]]:
    """Hand-curated correlation buckets, derived from the catalog as of
    2026-05-06. Update as the symbol universe changes.

    A cluster is a group of symbols whose returns move together enough
    that holding multiple positions effectively doubles your bet on the
    same risk factor. Empirically:

      - Metals: XAUUSD/XAGUSD/XPTUSD/XPDUSD all correlate strongly with
        USD strength (DXY) and inflation expectations. Pearson r often
        > 0.6 on weekly returns.
      - US indices: US100/US500/US30 all correlate > 0.8 (basket of
        the same 30-500 large-cap stocks with overlap). Add US tech
        names (TSLA/NVDA/etc.) when present.
      - Eurozone indices: GER40/FRA40/UK100 r > 0.7
      - Japan: JP225 by itself (low correlation to US/EU)
      - Commodities (energy/oil): WTI/Brent/natgas if present
      - Forex majors share USD as base — cluster by counter-currency

    The cap is per-cluster, applied to the COUNT of positions, not the
    notional. For more granularity use risk_clusters() below.
    """
    return {
        "metals_long":     ["XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"],
        "us_indices":      ["US100.cash", "US500.cash", "US30.cash",
                             "TSLA", "NVDA", "AAPL", "MSFT", "GOOGL",
                             "AMZN", "AVGO", "INTC"],
        "europe_indices":  ["GER40.cash", "FRA40.cash", "UK100.cash"],
        "japan_indices":   ["JP225.cash"],
        "fx_usd_majors":   ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],
        "fx_jpy":          ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY"],
        "crypto":          ["BTCUSD", "ETHUSD"],
    }


def find_cluster(symbol: str,
                  clusters: Mapping[str, list[str]]) -> str | None:
    """Return the cluster name that contains `symbol`, or None.

    A symbol can technically belong to multiple clusters; this returns
    the FIRST match in iteration order, which is fine for our use
    because the default_clusters list is structured so each symbol
    appears in exactly one cluster.
    """
    for name, members in clusters.items():
        if symbol in members:
            return name
    return None


# ---------------------------------------------------------------------------
# Cap check
# ---------------------------------------------------------------------------

def check_cluster_open(
    *,
    symbol: str,
    open_positions: Iterable[OpenPosition],
    clusters: Mapping[str, list[str]] | None = None,
    max_concurrent_per_cluster: int = 2,
) -> ClusterResult:
    """Decide whether opening on `symbol` would breach its cluster's
    concurrent-position cap.

    Args:
      symbol: the symbol the next signal wants to trade.
      open_positions: every currently-open position across ALL
                       deployments (paper + live).
      clusters: cluster name → list of symbols. Default: default_clusters()
      max_concurrent_per_cluster: max number of positions that can be
                       held simultaneously across symbols in any one
                       cluster. 2 is a reasonable default — lets the
                       book run two metals positions but not four.

    Returns ALLOW if symbol is in no cluster, or if the cluster has
    fewer than the cap currently open. BLOCK otherwise, with the
    cluster name and counts in `reason` for logging.
    """
    if clusters is None:
        clusters = default_clusters()

    cluster_name = find_cluster(symbol, clusters)
    if cluster_name is None:
        return ClusterResult(
            decision="ALLOW", cluster_name=None,
            current_count=0, cap=max_concurrent_per_cluster,
            reason=f"{symbol} is not in any correlation cluster",
        )

    members = set(clusters[cluster_name])
    current = sum(1 for p in open_positions if p.symbol in members)

    if current < max_concurrent_per_cluster:
        return ClusterResult(
            decision="ALLOW", cluster_name=cluster_name,
            current_count=current, cap=max_concurrent_per_cluster,
            reason=(f"cluster {cluster_name!r} has {current}/"
                     f"{max_concurrent_per_cluster} positions, "
                     f"opening {symbol} OK"),
        )
    return ClusterResult(
        decision="BLOCK", cluster_name=cluster_name,
        current_count=current, cap=max_concurrent_per_cluster,
        reason=(f"cluster {cluster_name!r} at cap "
                 f"({current}/{max_concurrent_per_cluster}); "
                 f"refusing to open {symbol} until one closes"),
    )


# ---------------------------------------------------------------------------
# Reporting helpers (for dashboards / Operations page)
# ---------------------------------------------------------------------------

def cluster_status(
    open_positions: Iterable[OpenPosition],
    clusters: Mapping[str, list[str]] | None = None,
    cap: int = 2,
) -> dict[str, dict]:
    """Return per-cluster utilisation snapshot.

    Output: {cluster_name: {"open": int, "cap": int,
                              "symbols": [open_symbols]}}.
    """
    if clusters is None:
        clusters = default_clusters()
    open_list = list(open_positions)
    out: dict[str, dict] = {}
    for name, members in clusters.items():
        member_set = set(members)
        held = [p.symbol for p in open_list if p.symbol in member_set]
        out[name] = {
            "open": len(held),
            "cap": cap,
            "at_cap": len(held) >= cap,
            "symbols": held,
        }
    return out
