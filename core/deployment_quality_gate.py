"""
deployment_quality_gate.py — block bad-edge strategies from deploying.

Three-tier verdict (OK / WARN / BLOCK) computed from an EdgeStat:

  OK     — every quality threshold met. Deploy without prompting.
  WARN   — minor concerns (small OOS sample, slightly unfavourable R:R).
            Caller may show a warning + require an explicit "I understand"
            confirmation before deploying.
  BLOCK  — fundamental failure: no positive edge, drawdown beyond the
            FTMO floor, recovery never achieved, etc. Refuse entirely.
            BLOCK can ONLY be overridden by the user setting
            `allow_block_override=True` in the call.

Why this exists:
  Without a quality gate, anyone can drag a `~ medium` confidence cell
  with 35% win rate and 1:1 R:R into Live, and watch it bleed money
  forever. The gate codifies the rules an experienced trader would
  apply intuitively, so a bad cell can't slip into Live by accident.

Default thresholds match an FTMO 100K Challenge (10% drawdown floor,
8% profit target). Each is per-account configurable via JSON next to
the account's other configs.

Used by:
  • dashboards/pages/7_🏛️_Strategy_Library.py — _add_single_to_account
  • dashboards/pages/8_⚔️_Strategy_Compare.py — _render_deploy_dialog
  • dashboards/pages/A_📦_Portfolio_Composer.py — _render_deploy_dialog
  • dashboards/components/deployment_dialogs.py — Operations Go-Live modal
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from core import account_manager, edge_catalog


Verdict = Literal["OK", "WARN", "BLOCK"]


@dataclass(frozen=True)
class QualityCriteria:
    """Per-account quality thresholds. Numbers are HARD floors / ceilings —
    a cell that misses any of them cannot deploy live without explicit
    override."""
    # Edge fundamentals
    min_expectancy_per_R: float = 0.0       # BLOCK if Trendo EV <= 0
    require_trendo_not_red: bool = True     # BLOCK if Trendo zone == 'red'

    # Win rate / R:R combo
    min_win_rate_pct: float = 35.0          # BLOCK below this
    min_rr_ratio: float = 1.0               # BLOCK below this (R:R below 1:1
                                              # only profitable at very high WR)

    # Drawdown / recovery
    max_dd_pct: float = 10.0                # BLOCK if DD ≥ FTMO floor
    max_recovery_days: float = 200.0        # BLOCK if longer than this
    require_recovered: bool = True          # BLOCK if recovery_days is None
                                              # (never recovered from worst DD)

    # Streak risk
    max_consec_losses: int = 8              # BLOCK above this
    warn_consec_losses: int = 5             # WARN at or above

    # Sample size confidence
    min_n_test: int = 10                    # BLOCK below this
    warn_n_test: int = 30                   # WARN below this (small sample)

    # FTMO Monte-Carlo pass rate (when present)
    min_p_pass_30d: float = 0.50            # BLOCK below 50% if known
    warn_p_pass_30d: float = 0.80           # WARN below 80%

    # Per-cell-loss vs FTMO daily cap (max consec × suggested risk)
    warn_streak_loss_pct_of_floor: float = 0.5
    """If max_consec × suggested_risk > 50% of FTMO floor, WARN."""

    @classmethod
    def default(cls) -> "QualityCriteria":
        return cls()

    @classmethod
    def lenient(cls) -> "QualityCriteria":
        """Looser thresholds — for paper deployment or experimentation."""
        return cls(
            min_win_rate_pct=30.0,
            min_rr_ratio=0.8,
            max_dd_pct=15.0,
            max_recovery_days=400.0,
            require_recovered=False,
            max_consec_losses=12,
            min_n_test=5,
            min_p_pass_30d=0.30,
        )


@dataclass(frozen=True)
class QualityResult:
    verdict: Verdict
    block_reasons: list[str] = field(default_factory=list)
    warn_reasons: list[str] = field(default_factory=list)
    pass_notes: list[str] = field(default_factory=list)

    @property
    def is_ok(self) -> bool:
        return self.verdict == "OK"

    @property
    def is_blocked(self) -> bool:
        return self.verdict == "BLOCK"

    @property
    def needs_override(self) -> bool:
        """True iff the user must explicitly confirm before deploying."""
        return self.verdict in ("WARN", "BLOCK")

    def summary_line(self) -> str:
        """One-line summary for toasts / log entries."""
        if self.verdict == "OK":
            return f"✅ Quality OK ({len(self.pass_notes)} checks passed)"
        if self.verdict == "WARN":
            return (f"🟡 Quality WARN ({len(self.warn_reasons)} concern(s)): "
                    + " · ".join(self.warn_reasons[:3]))
        return (f"⛔ Quality BLOCK ({len(self.block_reasons)} failure(s)): "
                + " · ".join(self.block_reasons[:3]))


def evaluate_quality(
    es: edge_catalog.EdgeStat,
    *,
    criteria: QualityCriteria | None = None,
    suggested_risk_pct: float | None = None,
    ftmo_floor_pct: float = 10.0,
) -> QualityResult:
    """Run the strategy through every quality check.

    `suggested_risk_pct` and `ftmo_floor_pct` are optional — when both
    are provided, the gate also checks the streak-loss × risk product.

    Order of severity (BLOCK conditions checked first; first failure
    wins for the headline reason but ALL failures are returned).
    """
    cfg = criteria or QualityCriteria.default()
    block: list[str] = []
    warn: list[str] = []
    passed: list[str] = []

    # ── BLOCK rules — these are non-negotiable ─────────────────────
    # 1. Edge expectancy (Trendo classifier)
    if cfg.min_expectancy_per_R is not None:
        ev = es.expectancy_per_R
        if ev < cfg.min_expectancy_per_R:
            block.append(
                f"NO POSITIVE EDGE — EV per R is "
                f"{ev:+.3f} (min required {cfg.min_expectancy_per_R:+.3f}). "
                f"Math says this strategy loses money long-run regardless "
                f"of how it performed on this sample."
            )
        else:
            passed.append(f"EV per R {ev:+.3f}R ≥ "
                            f"{cfg.min_expectancy_per_R:+.3f}")

    # 2. Trendo zone red — fundamental failure
    if cfg.require_trendo_not_red and es.trendo_zone == "red":
        block.append(
            f"TRENDO RED ZONE — win rate {es.win_rate_pct:.1f}% combined "
            f"with R:R {es.rr_ratio:.2f} produces negative EV. "
            f"Mathematically unprofitable in expectation."
        )
    elif es.trendo_zone == "green":
        passed.append("Trendo zone GREEN")

    # 3. Win rate floor
    if es.win_rate_pct < cfg.min_win_rate_pct:
        block.append(
            f"WIN RATE TOO LOW — {es.win_rate_pct:.1f}% < "
            f"floor {cfg.min_win_rate_pct:.1f}%."
        )
    else:
        passed.append(f"Win rate {es.win_rate_pct:.1f}% ≥ "
                        f"{cfg.min_win_rate_pct:.0f}%")

    # 4. R:R floor
    if es.rr_ratio and es.rr_ratio < cfg.min_rr_ratio:
        block.append(
            f"R:R TOO LOW — {es.rr_ratio:.2f} < min {cfg.min_rr_ratio:.2f}. "
            f"Below 1:1 needs >50% win rate just to break even after fees."
        )

    # 5. Drawdown vs FTMO floor
    if es.max_dd_pct > cfg.max_dd_pct:
        block.append(
            f"HISTORICAL DRAWDOWN BUSTS FTMO — max DD "
            f"{es.max_dd_pct:.2f}% > floor {cfg.max_dd_pct:.2f}%. "
            f"This strategy already lost more than the FTMO challenge "
            f"limit on backtest data."
        )
    else:
        passed.append(f"Max DD {es.max_dd_pct:.2f}% ≤ "
                        f"{cfg.max_dd_pct:.0f}%")

    # 6. Recovery time (when recovery happened)
    if es.recovery_days is not None:
        if es.recovery_days > cfg.max_recovery_days:
            block.append(
                f"RECOVERY TOO SLOW — {es.recovery_days:.0f} days "
                f"underwater > max {cfg.max_recovery_days:.0f}d. Even if "
                f"the edge recovers eventually, you'd starve the FTMO "
                f"clock first."
            )
        else:
            passed.append(f"Recovery {es.recovery_days:.0f}d ≤ "
                            f"{cfg.max_recovery_days:.0f}d")
    elif cfg.require_recovered and es.max_dd_pct > 1.0:
        block.append(
            f"NEVER RECOVERED — historical drawdown of "
            f"{es.max_dd_pct:.2f}% has not been recouped to a new high "
            f"in the entire OOS window. The strategy may still be in DD."
        )

    # 7. Sample size (BLOCK if absurdly small)
    if es.n_test < cfg.min_n_test:
        block.append(
            f"SAMPLE TOO SMALL — only {es.n_test} OOS trades < "
            f"min {cfg.min_n_test}. Numbers are not statistically reliable."
        )

    # 8. FTMO Monte-Carlo pass rate floor (when computed)
    if (es.p_pass_30d is not None
        and es.p_pass_30d < cfg.min_p_pass_30d):
        block.append(
            f"FTMO PASS RATE TOO LOW — "
            f"{es.p_pass_30d * 100:.0f}% < {cfg.min_p_pass_30d * 100:.0f}%. "
            f"Monte Carlo says you'd bust the challenge more than half "
            f"the runs."
        )

    # 9. Catastrophic streak (BLOCK above hard ceiling)
    if es.max_consec_losses >= cfg.max_consec_losses:
        block.append(
            f"LOSING STREAK TOO LONG — {es.max_consec_losses} consecutive "
            f"losses ≥ ceiling {cfg.max_consec_losses}. Drawdown × this "
            f"streak likely busts the account in any single bad week."
        )

    # 10. Streak loss vs FTMO floor (when risk supplied)
    if (suggested_risk_pct is not None and ftmo_floor_pct > 0
        and es.max_consec_losses > 0):
        streak_loss_pct = suggested_risk_pct * es.max_consec_losses
        if streak_loss_pct >= ftmo_floor_pct:
            block.append(
                f"STREAK × RISK BUSTS FTMO — "
                f"{suggested_risk_pct:.2f}% × "
                f"{es.max_consec_losses} losses = "
                f"{streak_loss_pct:.2f}% ≥ floor {ftmo_floor_pct:.0f}%. "
                f"A single losing streak would bust the account."
            )
        elif streak_loss_pct >= cfg.warn_streak_loss_pct_of_floor * ftmo_floor_pct:
            warn.append(
                f"streak × risk = {streak_loss_pct:.2f}% — "
                f"close to FTMO floor"
            )

    # ── WARN rules — caller can override ──────────────────────────────
    if (cfg.warn_consec_losses > 0
        and es.max_consec_losses >= cfg.warn_consec_losses
        and es.max_consec_losses < cfg.max_consec_losses):
        warn.append(
            f"max consec losses {es.max_consec_losses} ≥ warn "
            f"{cfg.warn_consec_losses}"
        )

    if (cfg.warn_n_test > 0 and es.n_test < cfg.warn_n_test
        and es.n_test >= cfg.min_n_test):
        warn.append(
            f"small OOS sample n={es.n_test} (< {cfg.warn_n_test}) — "
            f"results are noisier"
        )

    if (es.p_pass_30d is not None
        and es.p_pass_30d < cfg.warn_p_pass_30d
        and es.p_pass_30d >= cfg.min_p_pass_30d):
        warn.append(
            f"FTMO pass {es.p_pass_30d * 100:.0f}% < "
            f"warn threshold {cfg.warn_p_pass_30d * 100:.0f}%"
        )

    if es.trendo_zone == "amber" and not block:
        warn.append("Trendo zone AMBER — break-even-ish edge")

    # ── Verdict ────────────────────────────────────────────────────
    verdict: Verdict
    if block:
        verdict = "BLOCK"
    elif warn:
        verdict = "WARN"
    else:
        verdict = "OK"

    return QualityResult(
        verdict=verdict,
        block_reasons=block,
        warn_reasons=warn,
        pass_notes=passed,
    )


# ─────────────────────────────────────────────────────────────────────
# Per-account criteria persistence
# ─────────────────────────────────────────────────────────────────────

def _criteria_path(login: int) -> Path:
    base = account_manager.get_deployments_path(login).parent
    return base / "quality_gate.json"


def load_criteria(login: int) -> QualityCriteria:
    """Load per-account quality criteria. Falls back to default."""
    p = _criteria_path(login)
    if not p.exists():
        return QualityCriteria.default()
    try:
        return QualityCriteria(**json.loads(p.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError):
        return QualityCriteria.default()


def save_criteria(login: int, cfg: QualityCriteria) -> None:
    p = _criteria_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
