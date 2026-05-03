"""
deployment.py — Deployment dataclass + per-account persistence.

A "deployment" is a single (strategy, ticker, tf, params, risk_pct, daily_cap_pct)
configuration tied to one account. The Operations page renders one card per
deployment; the Go-Live modal acts on a single deployment at a time.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from core import account_manager


DeploymentStatus = Literal["idle", "paper", "live", "paused", "halted"]


@dataclass
class Deployment:
    """One row in the operator's portfolio for a specific account."""
    deployment_id: str            # 'vol_breakout_US100.cash_D1' (slug)
    strategy: str                  # strategy.name
    ticker: str
    tf: str
    long_only: bool = True
    params: dict = field(default_factory=dict)
    risk_pct: float = 0.3
    daily_cap_pct: float = 1.0
    status: DeploymentStatus = "idle"
    last_started_at_utc: str | None = None
    last_signal_at_utc: str | None = None
    paper_run_id: str | None = None
    live_run_id: str | None = None
    notes: str = ""

    @classmethod
    def slug(cls, strategy: str, ticker: str, tf: str) -> str:
        # Replace '.' so the slug is filesystem-safe
        safe_ticker = ticker.replace(".", "_")
        return f"{strategy}_{safe_ticker}_{tf}"


def load_deployments(login: int) -> list[Deployment]:
    path = account_manager.get_deployments_path(login)
    if not path.exists():
        return []
    rows = json.loads(path.read_text())
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a JSON array")
    return [Deployment(**r) for r in rows]


def save_deployments(login: int, deployments: list[Deployment]) -> None:
    path = account_manager.get_deployments_path(login)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(d) for d in deployments]
    path.write_text(json.dumps(payload, indent=2) + "\n")


def upsert_deployment(login: int, dep: Deployment) -> Deployment:
    """Insert or update by deployment_id."""
    existing = load_deployments(login)
    by_id = {d.deployment_id: d for d in existing}
    by_id[dep.deployment_id] = dep
    save_deployments(login, list(by_id.values()))
    return dep


def remove_deployment(login: int, deployment_id: str) -> bool:
    existing = load_deployments(login)
    new_list = [d for d in existing if d.deployment_id != deployment_id]
    if len(new_list) == len(existing):
        return False
    save_deployments(login, new_list)
    return True


def update_status(login: int, deployment_id: str, status: DeploymentStatus,
                  *, paper_run_id: str | None = None,
                  live_run_id: str | None = None) -> Deployment | None:
    deployments = load_deployments(login)
    for d in deployments:
        if d.deployment_id == deployment_id:
            d.status = status
            if status in ("paper", "live"):
                d.last_started_at_utc = datetime.now(timezone.utc).isoformat()
            if paper_run_id is not None:
                d.paper_run_id = paper_run_id
            if live_run_id is not None:
                d.live_run_id = live_run_id
            save_deployments(login, deployments)
            return d
    return None


def seed_survivor_deployments(login: int) -> list[Deployment]:
    """Pre-fill an account with the recommended portfolio from
    `core.strategy_library` — the survivors backed by real grid stats.
    Idempotent — re-running adds nothing new.

    Falls back to the static list below if the library is empty (no
    grid_results.md). The fallback uses ema_cross + donchian on the
    proven D1 cells.
    """
    try:
        from core import strategy_library
        lib = [e for e in strategy_library.list_library() if e.recommended]
    except Exception:
        lib = []

    existing = {d.deployment_id for d in load_deployments(login)}
    deps: list[Deployment] = []

    if lib:
        for entry in lib:
            slug = (Deployment.slug(entry.strategy, entry.ticker, entry.tf)
                    + ("_long" if entry.long_only else "_bidir"))
            if slug in existing:
                continue
            # Pre-fill params with long_only and any other params the
            # strategy supports (parsed from the variant suffix).
            params: dict[str, Any] = {"long_only": entry.long_only}
            deps.append(Deployment(
                deployment_id=slug,
                strategy=entry.strategy,
                ticker=entry.ticker, tf=entry.tf,
                long_only=entry.long_only,
                params=params,
                risk_pct=0.3, daily_cap_pct=1.0,
                status="idle",
                notes=entry.why or "",
            ))
    else:
        # Static fallback — exact strategy names with their params.
        # Each row: (strategy, ticker, tf, long_only, why)
        fallback = [
            ("ema_cross_9_20",  "USDJPY",     "D1", True,
             "Strongest test_R survivor on D1 cross-pair."),
            ("ema_cross_12_26", "GBPJPY",     "D1", True,
             "Second-strongest D1 cross-pair."),
            ("ema_cross_9_20",  "GBPUSD",     "D1", True,
             "Cleanest D1 cross — 13 OOS trades."),
            ("donchian_20",     "US100.cash", "D1", True,
             "Index breakout — 19 OOS trades."),
        ]
        for strategy, ticker, tf, long_only, why in fallback:
            slug = (Deployment.slug(strategy, ticker, tf)
                    + ("_long" if long_only else "_bidir"))
            if slug in existing:
                continue
            deps.append(Deployment(
                deployment_id=slug, strategy=strategy,
                ticker=ticker, tf=tf, long_only=long_only,
                params={"long_only": long_only},
                risk_pct=0.3, daily_cap_pct=1.0,
                status="idle", notes=why,
            ))

    if deps:
        merged = load_deployments(login) + deps
        save_deployments(login, merged)
    return load_deployments(login)
