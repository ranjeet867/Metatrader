"""
risk_engine.py — position sizing + per-strategy risk state.

Two responsibilities, kept separate:

  PositionSizer  — pure math. lots_for_risk(balance, risk_pct, stop_distance,
                    money_per_unit, volume_step) → lots.
  LiveRiskTracker — stateful. Records trade closures and decides whether
                    the next trade can fire (consecutive losses + cooldown
                    + daily loss cap). Persists state to data/v2.db so
                    restarts recover the cap.

INVARIANT-7: no clock-based seeding; all randomness (none here) would use
a seeded RNG. INVARIANT-6: live_executor calls into LiveRiskTracker; refusals
are logged with reason.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from core import storage


# ---------------------------------------------------------------------------
# PositionSizer — pure math
# ---------------------------------------------------------------------------

@dataclass
class PositionSizer:
    """Compute lots for a given $ risk budget.

    lots_for_risk = (account_balance × risk_pct/100) / (stop_distance × money_per_unit)

    Result is rounded DOWN to the nearest volume_step (e.g. 0.01 lot).
    """

    @staticmethod
    def lots_for_risk(account_balance: float, risk_pct: float,
                      stop_distance: float, money_per_unit: float,
                      volume_step: float = 0.01,
                      volume_min: float = 0.01) -> float:
        if account_balance <= 0:
            return 0.0
        if stop_distance <= 0 or money_per_unit <= 0:
            return 0.0
        if volume_step <= 0:
            raise ValueError(f"volume_step must be > 0; got {volume_step}")
        risk_dollars = account_balance * (risk_pct / 100.0)
        raw_lots = risk_dollars / (stop_distance * money_per_unit)
        # Round DOWN to volume_step
        lots = math.floor(raw_lots / volume_step) * volume_step
        # Clamp to volume_min — but if raw_lots < volume_min, return 0 (can't trade)
        if raw_lots < volume_min:
            return 0.0
        if lots < volume_min:
            lots = volume_min
        # round to volume_step's precision so we don't get FP noise
        ndigits = max(0, -int(math.floor(math.log10(volume_step)))) if volume_step < 1 else 0
        return round(lots, ndigits)


# ---------------------------------------------------------------------------
# LiveRiskTracker — persistent
# ---------------------------------------------------------------------------

RiskOutcome = Literal["allow", "deny"]


@dataclass(frozen=True)
class RiskDecision:
    outcome: RiskOutcome
    reason: str

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


class LiveRiskTracker:
    """Persistent per-(symbol, strategy) risk state.

    Backed by data/v2.db's risk_state + ftmo_daily_resets tables. Anything
    that lives across restarts goes through SQLite — INVARIANT-6 prevents the
    "in-memory state lost on restart bypassed our limits" v1-class bug.

    Args:
        db_path: SQLite path. The schema is initialised lazily.
        max_consecutive_losses: deny new trades after this many losses.
        cooldown_minutes: minutes after the cap is hit before allowing again.
        daily_loss_cap_pct: deny ALL trades when the account is down by this %
                            since the last FTMO reset.
    """

    def __init__(self, *, db_path: str | Path,
                 max_consecutive_losses: int = 3,
                 cooldown_minutes: int = 240,
                 daily_loss_cap_pct: float = 4.5):
        self.db_path = Path(db_path)
        self.max_consecutive_losses = int(max_consecutive_losses)
        self.cooldown_minutes = int(cooldown_minutes)
        self.daily_loss_cap_pct = float(daily_loss_cap_pct)
        storage.init_schema(self.db_path)

    # --- read ---

    def state_for(self, symbol: str, strategy: str) -> dict:
        s = storage.get_risk_state(self.db_path, symbol, strategy)
        if s is None:
            return {
                "symbol": symbol, "strategy": strategy,
                "consecutive_losses": 0, "last_loss_at_utc": None,
                "cooldown_until_utc": None, "daily_loss_pct": 0.0,
                "day_start_balance": None,
            }
        return s

    # --- write ---

    def record_trade_close(self, *, symbol: str, strategy: str,
                            r_multiple: float, pnl: float,
                            account_balance: float,
                            now_utc: datetime | None = None) -> dict:
        """Update state after a trade closes. Returns the new state dict."""
        now = now_utc or datetime.now(timezone.utc)
        cur = self.state_for(symbol, strategy)
        is_loss = pnl < 0
        consec = (cur["consecutive_losses"] + 1) if is_loss else 0
        last_loss = now.isoformat() if is_loss else cur["last_loss_at_utc"]

        cooldown_until = cur["cooldown_until_utc"]
        if is_loss and consec >= self.max_consecutive_losses:
            cooldown_until = (now + timedelta(minutes=self.cooldown_minutes)).isoformat()

        # Daily loss pct accumulates; reset_daily zeroes it.
        day_start = cur["day_start_balance"] or account_balance
        # If pnl < 0, daily_loss_pct GROWS (more negative).
        new_daily_loss = cur["daily_loss_pct"] + (-pnl / day_start * 100.0)

        storage.upsert_risk_state(
            self.db_path, symbol, strategy,
            consecutive_losses=consec, last_loss_at_utc=last_loss,
            cooldown_until_utc=cooldown_until,
            daily_loss_pct=new_daily_loss, day_start_balance=day_start,
        )
        return self.state_for(symbol, strategy)

    def reset_daily(self, *, at_utc: datetime, day_start_balance: float) -> None:
        """FTMO 22:00 UTC reset. Clears consecutive_losses + daily_loss_pct
        for ALL (symbol, strategy) rows; updates day_start_balance."""
        storage.reset_risk_state_daily(
            self.db_path, at_utc.isoformat(), day_start_balance
        )

    # --- decisions ---

    def account_daily_loss_pct(self) -> float:
        """Sum daily_loss_pct across all (symbol, strategy) rows.
        This is the account-wide loss since the last FTMO reset."""
        with storage.connect(self.db_path) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(daily_loss_pct), 0) FROM risk_state"
            ).fetchone()
        return float(row[0])

    def pre_trade_check(self, *, symbol: str, strategy: str,
                         now_utc: datetime | None = None) -> RiskDecision:
        """Decide whether a new trade can be opened on this (symbol, strategy)."""
        now = now_utc or datetime.now(timezone.utc)
        s = self.state_for(symbol, strategy)

        # Daily loss cap is ACCOUNT-WIDE (sum across all cells)
        acct_daily = self.account_daily_loss_pct()
        if acct_daily >= self.daily_loss_cap_pct:
            return RiskDecision(
                outcome="deny",
                reason=f"daily_loss_pct={acct_daily:.2f}% "
                        f">= cap {self.daily_loss_cap_pct:.2f}%",
            )

        # Cooldown timer
        if s["cooldown_until_utc"]:
            cd = datetime.fromisoformat(s["cooldown_until_utc"])
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=timezone.utc)
            if now < cd:
                remain = (cd - now).total_seconds() / 60.0
                return RiskDecision(
                    outcome="deny",
                    reason=f"in cooldown for {remain:.1f} more minutes "
                            f"(after {s['consecutive_losses']} losses)",
                )

        # Consecutive losses (independent of cooldown — a tracker may have
        # consec=N but cooldown already expired; allow another attempt)
        return RiskDecision(outcome="allow", reason="ok")
