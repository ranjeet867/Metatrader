"""
adaptive_risk.py — auto-mode risk-pct sizing that scales with FTMO buffer.

The problem this solves
-----------------------
A static `risk_pct = 0.30` is wrong in three regimes:

1. ACCOUNT IN DEEP DRAWDOWN: equity is 1% above the FTMO total-loss
   floor. A single full-stop trade at 0.30% × current-equity is fine
   in absolute $ but uses 30% of the remaining buffer. One bad day =
   account dead. Static sizing doesn't shrink.

2. ACCOUNT IN PROFIT WITH HEADROOM: equity is well above baseline.
   Strategy could deploy more aggressive sizing because today's loss
   limit is far away. Static sizing leaves edge on the table.

3. PARTIAL RECOVERY OSCILLATIONS: equity went 91k → 93k → 91k. The
   recovery wasn't durable. Static sizing keeps offering full risk
   even after evidence the regime is unstable.

Adaptive risk solves (1) and (2) by computing per-trade risk from the
SMALLER of the daily and total remaining buffers, divided by the
expected number of trades the user wants today. (3) is handled by a
separate `equity_tracker` module (out of scope for this file — see
the followup).

The math
--------
Given:
    current_equity              — broker account_info equity NOW
    baseline_equity             — FTMO start equity ($100k typical)
    daily_pnl_so_far            — today's realized P&L (signed; <0 = loss)
    daily_soft_cap_pct          — user's daily comfort cap (2% conservative)
    daily_hard_cap_pct          — FTMO daily-loss limit (5%)
    total_loss_floor_pct        — FTMO total-loss limit (10%)
    target_trades_per_day       — expected trade frequency
    max_risk_pct                — never exceed this regardless of buffer
    min_risk_pct                — floor below which we just skip

Compute:
    daily_soft_buffer_usd       = baseline × daily_soft_cap_pct/100
                                  + daily_pnl_so_far
        # ~"how much I'm willing to lose more today"
    daily_hard_buffer_usd       = baseline × daily_hard_cap_pct/100
                                  + daily_pnl_so_far
        # ~"how much before FTMO breaks the account"
    total_buffer_usd            = current_equity - baseline ×
                                  (1 - total_loss_floor_pct/100)
        # ~"how much before lifetime DD breaches"

    effective_buffer            = min of all three (tightest constraint)
    per_trade_risk_usd          = effective_buffer / target_trades_per_day
    adaptive_risk_pct           = (per_trade_risk_usd / current_equity)
                                  × 100, clamped to [min, max]

Returned AdaptiveRiskResult also tells the caller:
    - allow_trade               — False if any buffer ≤ 0
    - the binding constraint    — which of the 3 caps was tightest
    - max_trades_today_remaining — how many more trades fit in buffer
    - reason                    — human-readable string for log/UI

This is a PURE function — no I/O, no global state. Caller fetches
account_info from the bridge and passes everything in. Easy to test.

Future extensions (out of scope here):
    - account_in_recovery() flag from equity_tracker → multiply risk
      by a recovery_factor (e.g. 0.5) when in fragile recovery.
    - account_at_peak() flag → multiply by peak_factor (e.g. 1.5)
      when well above baseline.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class BindingConstraint(str, Enum):
    """Which buffer was tightest — drives caller's UI message + log."""
    DAILY_SOFT = "daily_soft_cap"        # 2% comfort cap (most often)
    DAILY_HARD = "daily_hard_cap"        # 5% FTMO daily limit
    TOTAL_FLOOR = "total_loss_floor"     # 10% FTMO lifetime limit
    MAX_RISK_CLAMP = "max_risk_clamp"    # Buffer is huge but max_risk caps
    NONE = "none"                        # Trade blocked (buffer <= 0)


@dataclass(frozen=True)
class AdaptiveRiskResult:
    """Output of calculate_adaptive_risk_pct.

    Caller pattern in runner._tick_deployment:

        result = adaptive_risk.calculate(
            current_equity=acct.equity,
            baseline_equity=acct.baseline_equity,
            daily_pnl_so_far=daily_realized_pnl,
            ...
        )
        if not result.allow_trade:
            log.warning("blocked: %s", result.reason)
            return
        # Use result.risk_pct in the position sizer
    """
    risk_pct: float                      # Final per-trade risk %
    allow_trade: bool                    # False = block all opens
    binding_constraint: BindingConstraint
    reason: str                          # Human-readable for log/UI

    # Diagnostics — surfaced in dashboard tile
    daily_soft_buffer_usd: float
    daily_hard_buffer_usd: float
    total_buffer_usd: float
    effective_buffer_usd: float          # min of three
    per_trade_risk_usd: float            # effective_buffer / target_trades
    max_trades_today_remaining: int
    raw_risk_pct: float                  # before min/max clamping


def calculate(
    *,
    current_equity: float,
    baseline_equity: float,
    daily_pnl_so_far: float,             # negative = loss
    daily_soft_cap_pct: float = 2.0,
    daily_hard_cap_pct: float = 5.0,
    total_loss_floor_pct: float = 10.0,
    target_trades_per_day: int = 5,
    max_risk_pct: float = 1.0,
    min_risk_pct: float = 0.05,
) -> AdaptiveRiskResult:
    """Compute the adaptive per-trade risk_pct given the FTMO state.

    Pure function. Returns an AdaptiveRiskResult struct with the
    final risk_pct + diagnostics + a binding-constraint tag.

    Defaults match a conservative FTMO Challenge:
        2% soft daily cap (40% of FTMO's 5% — leaves room for surprises)
        5% hard daily cap (FTMO breach point)
        10% total-loss floor
        5 expected trades/day
        max 1% per trade
        min 0.05% (below this, skip — not worth the noise)
    """
    # ── Compute three buffers ────────────────────────────────────────
    # Daily caps are anchored to BASELINE so a partial recovery doesn't
    # let you re-risk yesterday's losses. Total-floor is anchored to
    # CURRENT-equity vs the FTMO lifetime floor.
    daily_soft_buffer = (baseline_equity * daily_soft_cap_pct / 100.0
                          + daily_pnl_so_far)
    daily_hard_buffer = (baseline_equity * daily_hard_cap_pct / 100.0
                          + daily_pnl_so_far)
    total_floor_usd = baseline_equity * (1.0 - total_loss_floor_pct / 100.0)
    total_buffer = current_equity - total_floor_usd

    # ── Pick the tightest binding constraint ─────────────────────────
    # All three must be positive; if any ≤ 0 we refuse the trade.
    buffers = {
        BindingConstraint.DAILY_SOFT: daily_soft_buffer,
        BindingConstraint.DAILY_HARD: daily_hard_buffer,
        BindingConstraint.TOTAL_FLOOR: total_buffer,
    }
    if any(b <= 0 for b in buffers.values()):
        # Find which is the worst breach for the reason message
        worst = min(buffers.items(), key=lambda kv: kv[1])
        return AdaptiveRiskResult(
            risk_pct=0.0,
            allow_trade=False,
            binding_constraint=BindingConstraint.NONE,
            reason=(
                f"buffer exhausted: {worst[0].value} "
                f"= ${worst[1]:.2f}"
            ),
            daily_soft_buffer_usd=daily_soft_buffer,
            daily_hard_buffer_usd=daily_hard_buffer,
            total_buffer_usd=total_buffer,
            effective_buffer_usd=min(buffers.values()),
            per_trade_risk_usd=0.0,
            max_trades_today_remaining=0,
            raw_risk_pct=0.0,
        )

    # All buffers positive — pick the tightest (smallest) one.
    binding, effective_buffer = min(buffers.items(), key=lambda kv: kv[1])

    # ── Divide by target trades to get per-trade $ risk ──────────────
    target = max(1, int(target_trades_per_day))
    per_trade_risk_usd = effective_buffer / target

    # ── Convert to risk_pct of CURRENT equity (not baseline) ─────────
    # Why current: position-sizing math sizes the trade against the
    # current account balance. A risk_pct of 0.5% on $90k = $450, on
    # $110k = $550. We want the dollars right.
    raw_risk_pct = (per_trade_risk_usd / current_equity) * 100.0

    # ── Clamp to [min, max] ──────────────────────────────────────────
    risk_pct = max(min_risk_pct, min(max_risk_pct, raw_risk_pct))
    if risk_pct == max_risk_pct and raw_risk_pct > max_risk_pct:
        binding = BindingConstraint.MAX_RISK_CLAMP

    # ── Compute max trades today remaining for UI display ────────────
    # Floor at 1 (we always allow at least 1 trade if buffer > 0)
    max_trades_remaining = max(
        1, int(effective_buffer / max(1.0, per_trade_risk_usd))
    )

    return AdaptiveRiskResult(
        risk_pct=risk_pct,
        allow_trade=True,
        binding_constraint=binding,
        reason=(
            f"binding={binding.value} buffer=${effective_buffer:.0f} "
            f"per_trade=${per_trade_risk_usd:.0f} "
            f"({risk_pct:.2f}% of ${current_equity:,.0f}) "
            f"trades_left={max_trades_remaining}"
        ),
        daily_soft_buffer_usd=daily_soft_buffer,
        daily_hard_buffer_usd=daily_hard_buffer,
        total_buffer_usd=total_buffer,
        effective_buffer_usd=effective_buffer,
        per_trade_risk_usd=per_trade_risk_usd,
        max_trades_today_remaining=max_trades_remaining,
        raw_risk_pct=raw_risk_pct,
    )


# ─── Recovery / peak modes (stub — to be wired with equity_tracker) ──
# When the equity_tracker (separate module) detects:
#   - "fragile recovery" pattern (peak → DD → partial bounce → DD)
#     → multiply risk_pct by 0.5 to slow re-engagement
#   - "stable peak" (sustained equity above baseline)
#     → multiply by 1.5 (within max_risk_pct ceiling)


def apply_recovery_factor(
    base: AdaptiveRiskResult,
    *,
    recovery_factor: float = 1.0,
    max_risk_pct: float = 1.0,
) -> AdaptiveRiskResult:
    """Multiply the adaptive risk by a recovery factor (0.5 fragile,
    1.5 peak, 1.0 normal). Caller fetches the factor from the
    equity_tracker. Keeps adaptive_risk.py pure — no equity history
    coupling here."""
    if not base.allow_trade or recovery_factor == 1.0:
        return base
    new_pct = max(0.05, min(max_risk_pct, base.risk_pct * recovery_factor))
    return AdaptiveRiskResult(
        risk_pct=new_pct,
        allow_trade=base.allow_trade,
        binding_constraint=base.binding_constraint,
        reason=(base.reason + f" × recovery_factor={recovery_factor}"
                + f" → final={new_pct:.2f}%"),
        daily_soft_buffer_usd=base.daily_soft_buffer_usd,
        daily_hard_buffer_usd=base.daily_hard_buffer_usd,
        total_buffer_usd=base.total_buffer_usd,
        effective_buffer_usd=base.effective_buffer_usd,
        per_trade_risk_usd=base.per_trade_risk_usd,
        max_trades_today_remaining=base.max_trades_today_remaining,
        raw_risk_pct=base.raw_risk_pct,
    )
