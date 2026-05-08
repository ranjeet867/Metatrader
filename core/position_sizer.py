"""
position_sizer.py — pure-function dynamic lot sizing.

Replaces the v1 / early-v2 'fixed --lots' path. Computes EXACTLY how many lots
to trade given a target risk-percent of equity, the broker's tick metadata,
and the trade's stop distance.

INVARIANT-3-style: this is the SINGLE place lot sizing happens. backtest /
replay / paper / live all import calc_lots; no code path may compute lots
some other way.

Rejection cases (return SizingResult(ok=False, reason=...)):
  - invalid_inputs        — equity<=0 or risk_pct<=0
  - stop_equals_entry     — stop_distance == 0
  - tick_metadata_missing — tick_size or tick_value <= 0
  - money_per_lot_zero    — derived $/lot at SL is 0 (degenerate broker data)
  - below_min_lot_would_exceed_risk — broker's volume_min would risk >110% budget
  - rounding_overshoot    — even after volume_min handling, $risk > 110% budget
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolInfo:
    """Broker-side metadata for a symbol. Pulled from MT5 SymbolInfoXxx.

    Keep ALL fields populated — calc_lots refuses to size with missing data
    rather than guessing (INVARIANT-5).
    """
    name: str
    tick_size: float          # e.g. 0.01 (US100.cash) or 0.00001 (EURUSD)
    tick_value: float         # $ per tick per 1.0 lot, in account currency
    volume_step: float        # e.g. 0.1 for indices, 0.01 for FX
    volume_min: float
    volume_max: float
    digits: int
    contract_size: float


@dataclass(frozen=True)
class SizingResult:
    """Outcome of calc_lots. .ok tells the caller whether to trade."""
    ok: bool
    lots: float
    money_risk: float       # actual $ at SL given final lots
    intended_risk: float    # equity * risk_pct/100
    reason: str             # 'ok' or rejection code listed above

    def __bool__(self) -> bool:
        return self.ok


# Maximum overshoot tolerated when rounding lots up to volume_min
# (or after rounding down to volume_step). 10% overshoot ⇒ allow.
_OVERSHOOT_TOLERANCE = 1.10


def floor_to_step(value: float, step: float) -> float:
    """Round value DOWN to the nearest multiple of step.

    Includes a tiny epsilon so values that are mathematically AT a step
    boundary (e.g. raw=3.9 with step=0.1 producing 3.8999... due to FP)
    snap to the boundary, not below.
    """
    if step <= 0:
        raise ValueError(f"step must be > 0; got {step}")
    return math.floor((value + 1e-12) / step) * step


def _round_to_step_precision(value: float, step: float) -> float:
    """Trim FP noise so 3.9 prints as 3.9 not 3.8999999999999995."""
    if step <= 0:
        return value
    if step >= 1:
        ndigits = 0
    else:
        ndigits = max(0, -int(math.floor(math.log10(step))))
    return round(value, ndigits)


def calc_lots(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    sym: SymbolInfo,
    *,
    max_lots: float | None = None,
    max_money_risk_usd: float | None = None,
) -> SizingResult:
    """Compute exact lots from a risk-percent target.

    Steps:
      1. risk_amount  = equity * risk_pct / 100
         If `max_money_risk_usd` is set, risk_amount is HARD-CAPPED to that
         value. This is the safety net against tight-stop blow-ups: even
         if risk_pct × equity says you can lose $400, max_money_risk_usd
         of $300 forces lots small enough to risk only $300. Crucial for
         instruments like XAUUSD where 1 wrong lot = 5% account in a few
         ticks.
      2. money_per_lot_at_sl = (|entry-stop| / tick_size) * tick_value
      3. raw_lots     = risk_amount / money_per_lot_at_sl
      4. floored      = floor_to_step(raw_lots, volume_step)
      5. If floored < volume_min: bump to volume_min UNLESS that would
         exceed risk_amount * 1.10 → reject.
      6. If floored > volume_max: clamp DOWN to volume_max (rounded to step).
      7. If `max_lots` is provided AND floored > max_lots: clamp DOWN to
         max_lots (rounded to step). This is the user-side risk cap — your
         FTMO challenge may permit fewer lots than the symbol's volume_max.
      8. If actual_money_risk > risk_amount * 1.10 → reject (rounding_overshoot).
    """
    if equity <= 0 or risk_pct <= 0:
        return SizingResult(False, 0.0, 0.0, 0.0, "invalid_inputs")

    risk_amount = equity * risk_pct / 100.0
    # Hard $-ceiling: never let one trade risk more than this absolute amount.
    # This is the safety against tight-stop blow-ups on volatile instruments.
    if max_money_risk_usd is not None and max_money_risk_usd > 0:
        risk_amount = min(risk_amount, float(max_money_risk_usd))
    stop_distance = abs(entry_price - stop_price)

    if stop_distance <= 0:
        return SizingResult(False, 0.0, 0.0, risk_amount, "stop_equals_entry")
    if sym.tick_size <= 0 or sym.tick_value <= 0:
        return SizingResult(False, 0.0, 0.0, risk_amount, "tick_metadata_missing")

    money_per_lot_at_sl = (stop_distance / sym.tick_size) * sym.tick_value
    if money_per_lot_at_sl <= 0:
        return SizingResult(False, 0.0, 0.0, risk_amount, "money_per_lot_zero")

    raw_lots = risk_amount / money_per_lot_at_sl
    floored = floor_to_step(raw_lots, sym.volume_step)

    if floored < sym.volume_min:
        # Would rounding up to volume_min exceed our budget by > 10%?
        min_money_risk = sym.volume_min * money_per_lot_at_sl
        if min_money_risk > risk_amount * _OVERSHOOT_TOLERANCE:
            return SizingResult(False, 0.0, 0.0, risk_amount,
                                  "below_min_lot_would_exceed_risk")
        floored = sym.volume_min

    if floored > sym.volume_max:
        floored = floor_to_step(sym.volume_max, sym.volume_step)

    # User-side max-lots cap (FTMO challenges typically allow far fewer
    # lots than the broker's symbol volume_max).
    if max_lots is not None and max_lots > 0 and floored > max_lots:
        floored = floor_to_step(max_lots, sym.volume_step)
        # If the cap drops below volume_min, surface a clear rejection.
        if floored < sym.volume_min:
            return SizingResult(False, 0.0, 0.0, risk_amount,
                                  "max_lots_below_volume_min")

    floored = _round_to_step_precision(floored, sym.volume_step)
    actual_money_risk = floored * money_per_lot_at_sl

    if actual_money_risk > risk_amount * _OVERSHOOT_TOLERANCE:
        return SizingResult(False, 0.0, actual_money_risk, risk_amount,
                              "rounding_overshoot")

    return SizingResult(True, floored, actual_money_risk, risk_amount, "ok")
