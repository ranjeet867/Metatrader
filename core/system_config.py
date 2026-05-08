"""
system_config.py — single source of truth for ALL runtime configuration.

The problem this fixes
----------------------
Pre-this-module, "configuration" was scattered across:
  - data/risk_config.json          (account-wide risk caps + time guards)
  - data/cost_defaults.py          (cost model constants for backtests)
  - data/accounts/<login>/circuit_breaker.json
                                   (account circuit breaker thresholds)
  - data/accounts/<login>/deployments.json
                                   (per-deployment risk_pct, max_lots,
                                    max_money_risk_usd, daily_cap_pct)
  - several Streamlit pages each editing their own slice

Result: changing a setting in one page didn't propagate to others, and
no one knew which file was authoritative. Same logical setting (e.g.
"max risk per day") could end up with three different values in three
different files.

What this module does
---------------------
Provides ONE typed accessor — `SystemConfig` — that resolves all
config sources into a single read-mostly object. Every read API in the
codebase (runner, dashboard pages, scripts) should call
`load_system_config(login)` and read from the returned struct, never
re-loading individual JSON files.

Writes still go through the existing per-file save_* APIs (this is
deliberate — it preserves the existing UI write paths during migration).
But reads are unified, so a setting changed via any UI surface is
immediately visible to every reader on the next cache expiry (60s).

Invariants this guarantees
--------------------------
  - Every field has a defined default (loading from a missing/empty
    file produces a valid SystemConfig).
  - Schema-versioned via `_SCHEMA_VERSION` constant.
  - Per-account isolation — every load is keyed by login.
  - 60-second TTL cache so 1000 reads in a tight loop don't re-parse
    JSON 1000 times.
  - Frozen dataclass — runner can't accidentally mutate a tick-time
    read.
  - Readonly snapshot semantics — the struct returned reflects the
    state at load() time; later config changes don't retroactively
    affect already-fetched references.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from core import config as risk_config_mod
from core import cost_defaults

log = logging.getLogger(__name__)

# Bump when adding/renaming/removing fields in SystemConfig in a way
# that older callers should detect. We don't currently auto-migrate
# but legacy callers reading a newer config can warn.
_SCHEMA_VERSION = 1

# 60 second cache so that every tick of the live runner doesn't re-parse
# every JSON file. Dashboard writes invalidate by bumping the version
# stamp on disk; cache miss on next read.
_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class CostModel:
    """Backtest cost assumptions. Comes from `core.cost_defaults`.

    These are the SAME numbers used by:
      - run_backtest.py CLI
      - rebaseline_catalog.py
      - dashboards/pages/1_📊_Backtest.py
      - core/edge_catalog.py (when reading source_config_json metrics)

    Changing them in one place is supposed to flow to all of the above.
    Pre-fix some pages re-defined their own `commission` field and
    ignored cost_defaults — fix verified in tasks #178 #184 #185.
    """
    commission_usd: float
    slippage_atr_frac: float
    starting_balance_usd: float
    train_pct: float
    risk_pct_default: float


@dataclass(frozen=True)
class AccountRiskCaps:
    """Account-wide risk caps that apply ACROSS all deployments.

    Pre-fix several settings here (`daily_loss_cap_pct`,
    `max_open_positions`, `max_consecutive_losses`) were ALSO editable
    via per-deployment dialogs and via the Account Risk page. The
    dashboard had three places to set "max daily loss" — that's the
    config-sprawl bug. Now: this struct is the canonical home, and
    edits flow through `risk_config.json`.

    Fields use the same names as risk_config.json so migration is
    a 1:1 mapping.
    """
    weekend_flat_all: bool
    daily_close_flat_classes: tuple[str, ...]
    us_session_close_utc: str           # "HH:MM"
    flat_buffer_minutes: int
    no_entry_minutes_before_close: int
    daily_loss_cap_pct: float           # ACCOUNT-wide daily loss cap
    max_consecutive_losses: int
    max_open_positions: int
    ftmo_daily_reset_utc: str
    asset_class_overrides: dict[str, tuple[str, ...]]
    # Whether parity-freshness is enforced as a 24h gate or only at
    # deploy-time. Default is deploy-time only (False) per Phase-30d.
    enforce_parity_freshness: bool = False
    # ── Adaptive-risk auto-mode ──────────────────────────────────────
    # When True, the runner's per-tick risk_pct is replaced by
    # `core.adaptive_risk.calculate(...)` — auto-scaling sizing based
    # on remaining FTMO buffer. The static per-deployment risk_pct
    # becomes the MAXIMUM risk; adaptive shrinks it as buffer thins.
    # Default OFF (opt-in) so existing deployments keep current behavior.
    use_adaptive_risk: bool = False
    # User's preferred soft daily cap (vs FTMO's 5% hard cap). 2% is
    # conservative — gives room for 5 bad days before FTMO breaches.
    daily_soft_cap_pct: float = 2.0
    # Expected trades per day across all deployments. Used as the
    # divisor for adaptive risk: per_trade = remaining_buffer / target.
    target_trades_per_day: int = 5


@dataclass(frozen=True)
class DeploymentRiskCaps:
    """Per-deployment risk caps. These are PER-CELL, not account-wide.

    Set by the Composer at deploy-time via the deploy dialog. The
    runner's `_tick_deployment` reads them at every tick. Pre-fix the
    dashboard had two places to edit risk_pct — Composer dialog AND
    Operations card — and they could disagree.
    """
    risk_pct: float                     # 0.30 = 0.30% of equity per trade
    daily_cap_pct: float                # Per-deployment daily $ exposure cap
    max_lots: float                     # Hard upper bound on lots
    max_money_risk_usd: float           # Hard $-cap per trade (0 = no cap)


@dataclass(frozen=True)
class SystemConfig:
    """Top-level read-only view. All system configuration in one struct.

    Resolved from disk on load(). Cached for 60s. Every component that
    needs config should call `load_system_config(login)` and then read
    typed fields off the returned struct.

    Two scopes:
      - account-wide (`account_risk`, `cost_model`)
      - per-deployment, accessed via `deployment_caps(deployment_id)`
    """
    login: int
    schema_version: int
    loaded_at_unix: float
    cost_model: CostModel
    account_risk: AccountRiskCaps
    # Per-deployment caps keyed by deployment_id. Resolved lazily from
    # data/accounts/<login>/deployments.json. Empty dict if no deployments.
    deployment_caps_by_id: dict[str, DeploymentRiskCaps] = field(
        default_factory=dict
    )

    def deployment_caps(self, deployment_id: str) -> DeploymentRiskCaps:
        """Return caps for a specific deployment. Raises KeyError if
        the deployment doesn't exist (caller should check)."""
        return self.deployment_caps_by_id[deployment_id]

    def deployment_caps_or_default(
        self, deployment_id: str
    ) -> DeploymentRiskCaps:
        """Like deployment_caps but returns a sensible default if the
        deployment doesn't exist. Used by code that wants to size a
        hypothetical trade (e.g. preview-on-Composer)."""
        existing = self.deployment_caps_by_id.get(deployment_id)
        if existing is not None:
            return existing
        return DeploymentRiskCaps(
            risk_pct=self.cost_model.risk_pct_default,
            daily_cap_pct=1.0,
            max_lots=15.0,
            max_money_risk_usd=0.0,
        )


# ─── Cache ─────────────────────────────────────────────────────────────
# Per-account TTL cache. Each load with the same login returns the
# previously parsed struct unless 60s elapsed. The runner ticks every
# 5s now, so a 60s TTL means at most 12 cache hits between disk reads.
# Dashboard writes don't invalidate the cache directly — the next
# expiry picks up the change. If a write needs to be visible
# immediately call `invalidate(login)`.

_cache: dict[int, tuple[float, SystemConfig]] = {}


def invalidate(login: Optional[int] = None) -> None:
    """Force a re-read on next load. Call after any config write that
    needs to be visible immediately (e.g. dashboard save button)."""
    if login is None:
        _cache.clear()
        log.debug("system_config cache fully invalidated")
    else:
        _cache.pop(login, None)
        log.debug("system_config cache invalidated for login=%d", login)


def load_system_config(login: int) -> SystemConfig:
    """Load and return the resolved config for an account.

    Cached for 60 seconds per login. Call `invalidate(login)` to force
    a fresh read after writing.
    """
    cached = _cache.get(login)
    now = time.time()
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    cfg = _load_uncached(login)
    _cache[login] = (now, cfg)
    return cfg


def _load_uncached(login: int) -> SystemConfig:
    """Actually read from disk + build the struct. Don't call directly
    from tick paths — go through load_system_config so the cache works."""
    # Cost model — pure constants, never changes per account.
    cost = CostModel(
        commission_usd=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        starting_balance_usd=cost_defaults.DEFAULT_STARTING_BALANCE_USD,
        train_pct=cost_defaults.DEFAULT_TRAIN_PCT,
        risk_pct_default=cost_defaults.DEFAULT_RISK_PCT,
    )

    # Account risk — from risk_config.json. Falls back to defaults if
    # the file is malformed (load_config raises ValueError).
    try:
        rc = risk_config_mod.load_config()
        # Adaptive-risk fields live in the same risk_config.json under
        # a flat namespace. We read via raw fallback so legacy configs
        # without these keys don't break — defaults to OFF.
        raw = getattr(rc, "raw", {}) or {}
        account_risk = AccountRiskCaps(
            weekend_flat_all=rc.weekend_flat_all,
            daily_close_flat_classes=rc.daily_close_flat_classes,
            us_session_close_utc=rc.us_session_close_utc,
            flat_buffer_minutes=rc.flat_buffer_minutes,
            no_entry_minutes_before_close=rc.no_entry_minutes_before_close,
            daily_loss_cap_pct=rc.daily_loss_cap_pct,
            max_consecutive_losses=rc.max_consecutive_losses,
            max_open_positions=rc.max_open_positions,
            ftmo_daily_reset_utc=rc.ftmo_daily_reset_utc,
            asset_class_overrides=rc.asset_class_overrides,
            use_adaptive_risk=bool(raw.get("use_adaptive_risk", False)),
            daily_soft_cap_pct=float(raw.get("daily_soft_cap_pct", 2.0)),
            target_trades_per_day=int(raw.get("target_trades_per_day", 5)),
        )
    except Exception as e:
        log.warning("system_config: risk_config.json unreadable (%s) "
                    "— using safe defaults", e)
        account_risk = AccountRiskCaps(
            weekend_flat_all=True,
            daily_close_flat_classes=("stock", "index"),
            us_session_close_utc="20:00",
            flat_buffer_minutes=5,
            no_entry_minutes_before_close=30,
            daily_loss_cap_pct=4.5,
            max_consecutive_losses=3,
            max_open_positions=3,
            ftmo_daily_reset_utc="22:00",
            asset_class_overrides={},
        )

    # Per-deployment caps — from data/accounts/<login>/deployments.json.
    deployment_caps_by_id: dict[str, DeploymentRiskCaps] = {}
    try:
        from core import deployment as dep_mod
        deps = dep_mod.load_deployments(login)
        for d in deps:
            deployment_caps_by_id[d.deployment_id] = DeploymentRiskCaps(
                risk_pct=float(d.risk_pct or 0.0),
                daily_cap_pct=float(d.daily_cap_pct or 0.0),
                max_lots=float(d.max_lots or 0.0),
                max_money_risk_usd=float(d.max_money_risk_usd or 0.0),
            )
    except Exception as e:
        log.warning("system_config: deployments.json unreadable for "
                    "login=%d (%s) — no per-deployment caps loaded",
                    login, e)

    return SystemConfig(
        login=login,
        schema_version=_SCHEMA_VERSION,
        loaded_at_unix=time.time(),
        cost_model=cost,
        account_risk=account_risk,
        deployment_caps_by_id=deployment_caps_by_id,
    )


# ─── Convenience accessors ────────────────────────────────────────────
# These exist so callers can write `system_config.daily_loss_cap_pct(login)`
# instead of unpacking the whole struct. The single-line accessors are
# also the right place to add deprecation logging if a field name
# changes — one shim instead of N call sites.


def daily_loss_cap_pct(login: int) -> float:
    """Account-wide daily loss cap as a percentage of starting balance."""
    return load_system_config(login).account_risk.daily_loss_cap_pct


def max_open_positions(login: int) -> int:
    """Maximum simultaneous open positions across the account."""
    return load_system_config(login).account_risk.max_open_positions


def cost_model(login: int) -> CostModel:
    """Backtest cost assumptions — same view used by every code path
    that runs a backtest."""
    return load_system_config(login).cost_model


def get_summary_dict(login: int) -> dict[str, Any]:
    """Flatten to a dict for dashboard display + as_dict storage.

    NOT a complete schema dump — just the human-readable subset that
    fits in a settings panel. Use this in the unified Settings page so
    every value the user sees comes from one source."""
    cfg = load_system_config(login)
    return {
        "schema_version": cfg.schema_version,
        "loaded_at_unix": cfg.loaded_at_unix,
        # Cost model
        "cost.commission_usd": cfg.cost_model.commission_usd,
        "cost.slippage_atr_frac": cfg.cost_model.slippage_atr_frac,
        "cost.starting_balance_usd": cfg.cost_model.starting_balance_usd,
        "cost.train_pct": cfg.cost_model.train_pct,
        "cost.risk_pct_default": cfg.cost_model.risk_pct_default,
        # Account risk
        "risk.daily_loss_cap_pct": cfg.account_risk.daily_loss_cap_pct,
        "risk.max_consecutive_losses": cfg.account_risk.max_consecutive_losses,
        "risk.max_open_positions": cfg.account_risk.max_open_positions,
        "risk.no_entry_minutes_before_close":
            cfg.account_risk.no_entry_minutes_before_close,
        "risk.weekend_flat_all": cfg.account_risk.weekend_flat_all,
        "risk.us_session_close_utc": cfg.account_risk.us_session_close_utc,
        "risk.flat_buffer_minutes": cfg.account_risk.flat_buffer_minutes,
        "risk.ftmo_daily_reset_utc": cfg.account_risk.ftmo_daily_reset_utc,
        "risk.daily_close_flat_classes":
            list(cfg.account_risk.daily_close_flat_classes),
        # Deployment count
        "n_deployments": len(cfg.deployment_caps_by_id),
    }
