"""
circuit_breaker.py — system-wide stop-loss / target halt.

Per-account thresholds that, when breached, AUTO-HALT every active
deployment on that account. Read-only check (`evaluate`) called before
any signal opens a position; mutator (`enforce`) flips deployments to
`halted` status when it returns BLOCK.

Thresholds (all in dollars, all per-account):
  • daily_loss_dollars    — today's realised loss exceeds this → HALT
  • daily_target_dollars  — today's realised gain exceeds this → STOP_NEW
  • total_loss_dollars    — equity below baseline by this much → HALT
  • total_target_dollars  — equity above baseline by this much → STOP_NEW
  • max_consec_losses     — N losing trades in a row → HALT
  • max_open_positions    — across all deployments → STOP_NEW

Status returned:
  • OK           — proceed
  • STOP_NEW     — don't open new positions, but let existing run their SL/TP
  • HALT         — refuse all signals AND set all live deployments to halted
                    (paper deployments stay running so the user can keep
                    studying behaviour without real risk)

Why this exists:
  Without a circuit breaker, a fat-finger config or a strategy that
  goes bad mid-month can chain multiple losing trades together and
  bust an FTMO account before the user notices. This is the seatbelt.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from core import account_manager


CircuitState = Literal["OK", "STOP_NEW", "HALT"]


@dataclass(frozen=True)
class CircuitConfig:
    """Per-account thresholds. Set any field to None / 0 to disable that
    rule. Defaults match a conservative FTMO 100K Challenge."""
    daily_loss_dollars: float | None = 4_500.0     # FTMO 5% daily on $100k
    daily_target_dollars: float | None = None      # off by default
    total_loss_dollars: float | None = 9_000.0     # FTMO 10% total floor
    total_target_dollars: float | None = 8_000.0   # FTMO 8% profit target
    max_consec_losses: int | None = 5
    max_open_positions: int | None = 6
    halt_on_breach: bool = True                    # auto-pause deployments

    @classmethod
    def default(cls) -> "CircuitConfig":
        return cls()


@dataclass
class CircuitStatus:
    state: CircuitState
    reasons: list[str] = field(default_factory=list)
    realised_today: float = 0.0
    realised_total: float = 0.0
    open_positions: int = 0
    consec_losses: int = 0
    cfg: CircuitConfig = field(default_factory=CircuitConfig.default)
    evaluated_at_utc: str = ""

    @property
    def is_ok(self) -> bool:
        return self.state == "OK"

    @property
    def block_new(self) -> bool:
        return self.state in ("STOP_NEW", "HALT")


def _config_path(login: int) -> Path:
    base = account_manager.get_deployments_path(login).parent
    return base / "circuit_breaker.json"


def load_config(login: int) -> CircuitConfig:
    """Load per-account circuit-breaker config. Falls back to defaults
    when the file is missing — every account is protected on day one."""
    p = _config_path(login)
    if not p.exists():
        return CircuitConfig.default()
    try:
        data = json.loads(p.read_text())
        return CircuitConfig(**data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return CircuitConfig.default()


def save_config(login: int, cfg: CircuitConfig) -> None:
    p = _config_path(login)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(cfg), indent=2) + "\n")


def _today_realised(db_path: Path | str, mode: str) -> float:
    """Sum of realized_pnl for trades closed UTC-today in this mode."""
    p = Path(db_path)
    if not p.exists():
        return 0.0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(str(p)) as c:
            row = c.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0)
                   FROM trades
                   WHERE mode = ?
                     AND realized_pnl IS NOT NULL
                     AND substr(closed_at_utc, 1, 10) = ?""",
                (mode, today),
            ).fetchone()
            return float(row[0] or 0.0)
    except sqlite3.OperationalError:
        return 0.0


def _total_realised(db_path: Path | str, mode: str) -> float:
    """Sum of realized_pnl for all trades in this mode (lifetime)."""
    p = Path(db_path)
    if not p.exists():
        return 0.0
    try:
        with sqlite3.connect(str(p)) as c:
            row = c.execute(
                """SELECT COALESCE(SUM(realized_pnl), 0)
                   FROM trades
                   WHERE mode = ?
                     AND realized_pnl IS NOT NULL""",
                (mode,),
            ).fetchone()
            return float(row[0] or 0.0)
    except sqlite3.OperationalError:
        return 0.0


def _consec_losses(db_path: Path | str, mode: str) -> int:
    """Length of the current losing streak (tail of the trades table)."""
    p = Path(db_path)
    if not p.exists():
        return 0
    try:
        with sqlite3.connect(str(p)) as c:
            rows = c.execute(
                """SELECT realized_pnl FROM trades
                   WHERE mode = ? AND realized_pnl IS NOT NULL
                   ORDER BY closed_at_utc DESC, trade_idx DESC
                   LIMIT 50""",
                (mode,),
            ).fetchall()
    except sqlite3.OperationalError:
        return 0
    streak = 0
    for (pnl,) in rows:
        if pnl is None:
            continue
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def evaluate(
    db_path: Path | str,
    *,
    mode: str = "live",
    cfg: CircuitConfig | None = None,
    open_positions_count: int = 0,
) -> CircuitStatus:
    """Compute the current circuit state without modifying anything.

    The caller passes `open_positions_count` because the live-positions
    snapshot lives outside the SQLite journal (in MT5 / PaperExecutor).
    """
    cfg = cfg or CircuitConfig.default()
    realised_today = _today_realised(db_path, mode)
    realised_total = _total_realised(db_path, mode)
    streak = _consec_losses(db_path, mode)

    reasons: list[str] = []
    state: CircuitState = "OK"

    # Halt-level: total drawdown (most severe — protects the account)
    if cfg.total_loss_dollars and realised_total <= -float(cfg.total_loss_dollars):
        reasons.append(
            f"TOTAL LOSS ${-realised_total:,.0f} ≥ "
            f"limit ${cfg.total_loss_dollars:,.0f}"
        )
        state = "HALT"
    # Halt-level: daily loss
    if cfg.daily_loss_dollars and realised_today <= -float(cfg.daily_loss_dollars):
        reasons.append(
            f"DAILY LOSS ${-realised_today:,.0f} ≥ "
            f"limit ${cfg.daily_loss_dollars:,.0f}"
        )
        state = "HALT"
    # Halt-level: consecutive losing streak
    if cfg.max_consec_losses and streak >= int(cfg.max_consec_losses):
        reasons.append(
            f"CONSEC LOSSES {streak} ≥ limit {cfg.max_consec_losses}"
        )
        state = "HALT"

    # Stop-new-only: daily target hit, ride existing trades but no opens
    if (state == "OK" and cfg.daily_target_dollars
            and realised_today >= float(cfg.daily_target_dollars)):
        reasons.append(
            f"DAILY TARGET ${realised_today:,.0f} ≥ "
            f"target ${cfg.daily_target_dollars:,.0f} — locking gains"
        )
        state = "STOP_NEW"
    # Stop-new-only: total target hit
    if (state == "OK" and cfg.total_target_dollars
            and realised_total >= float(cfg.total_target_dollars)):
        reasons.append(
            f"TOTAL TARGET ${realised_total:,.0f} ≥ "
            f"target ${cfg.total_target_dollars:,.0f} — challenge passed"
        )
        state = "STOP_NEW"
    # Stop-new-only: at max-open-positions
    if (state == "OK" and cfg.max_open_positions
            and open_positions_count >= int(cfg.max_open_positions)):
        reasons.append(
            f"MAX OPEN POSITIONS {open_positions_count} ≥ "
            f"limit {cfg.max_open_positions}"
        )
        state = "STOP_NEW"

    return CircuitStatus(
        state=state,
        reasons=reasons,
        realised_today=realised_today,
        realised_total=realised_total,
        open_positions=open_positions_count,
        consec_losses=streak,
        cfg=cfg,
        evaluated_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def enforce(login: int, status: CircuitStatus) -> int:
    """If status.state == HALT and cfg.halt_on_breach, flip every
    `live`-status deployment on this account to `halted`. Returns the
    number of deployments halted. Paper deployments are LEFT ALONE so
    the user can keep observing strategy behaviour without real risk.
    """
    from core import deployment as dep_mod

    if status.state != "HALT" or not status.cfg.halt_on_breach:
        return 0
    deps = dep_mod.load_deployments(login)
    n_halted = 0
    for d in deps:
        if d.status == "live":
            d.status = "halted"
            d.notes = (
                f"[circuit_breaker {status.evaluated_at_utc}] "
                + " | ".join(status.reasons)
                + (f"\n{d.notes}" if d.notes else "")
            )
            dep_mod.save_deployments(login, deps)
            n_halted += 1
    if n_halted:
        # save once at the end (we mutated in-place, save once)
        dep_mod.save_deployments(login, deps)
    return n_halted
