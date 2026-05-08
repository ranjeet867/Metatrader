"""
preflight_tick.py — last-chance check that the SL we're about to send
is not ALREADY breached at the current market.

The problem this fixes
----------------------
Strategy fires on closed bar at 13:15:00 UTC, computes SL = 1.16898
(price was 1.17000 at close). Runner sends the order at 13:15:02. By
then the price has moved to 1.16895 — SL is already 0.3 pip into the
position. The position will open and IMMEDIATELY be in drawdown.

Backtest assumes "fill at signal bar's close price" = 1.17000. Live
fills at the current ask (or bid for shorts), which can be different
in either direction by a meaningful fraction of a pip on M15.

The fix
-------
Before sending the order, fetch the current tick price via the bridge.
Compute "would this position open already in drawdown of more than X
pips?" If yes, refuse the open. Caller logs and treats this as a
soft refusal — the bar window passes, no retry.

Why a separate module
---------------------
Live executor's send_order has 7 pre-flight checks already; this is
the 8th but it needs market data (tick) which the executor doesn't
hold. Easier to compose at the runner level than reach into the
executor for a one-off bridge call.

Threshold rationale
-------------------
We refuse if the entry price would be MORE THAN `max_drift_atr_frac`
× ATR past the signal entry. Default 0.10 = 10% of ATR. On EURUSD M15
with typical 5-pip ATR, that's a 0.5 pip tolerance — which matches
the catalog's modeled slippage of 0.05 ATR per fill (round trip 0.10).
If the price has drifted MORE than that, the live trade is no longer
representative of the backtest condition the catalog certified.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightResult:
    """Output of a pre-flight tick check.

    `should_open`: caller must respect this. False means refuse the open.
    `reason`: human-readable explanation if refused (or "ok" if pass).
    `current_bid`, `current_ask`: tick fetched from bridge — for logging.
    `signal_entry`: what the strategy said the entry would be.
    `drift_pips`: signed pips between signal entry and current market.
                  Positive = market moved IN FAVOR of the trade
                            (e.g. for LONG, current_ask < signal_entry).
                  Negative = market moved AGAINST.
    """
    should_open: bool
    reason: str
    current_bid: Optional[float]
    current_ask: Optional[float]
    signal_entry: float
    drift_pips: float = 0.0


def check(
    *,
    direction: str,                  # "LONG" or "SHORT"
    signal_entry_price: float,
    signal_stop_price: float,
    atr_at_signal: float,            # ATR value at the signal bar
    bridge_call: Callable,
    symbol: str,
    point_size: float = 0.0001,      # FX standard; pass 0.01 for indices
    max_drift_atr_frac: float = 0.10,
) -> PreflightResult:
    """Fetch the current tick and decide whether the open is still safe.

    Decision rules:
      1. Bridge fetch fails → fail-CLOSED (refuse open). Better to miss
         a trade than open one with stale price assumptions.
      2. Current ask (LONG) / bid (SHORT) is past the SL → refuse.
         Position would open already in drawdown.
      3. Drift exceeds max_drift_atr_frac × ATR → refuse.
         Backtest's slippage envelope is exceeded.
      4. Otherwise → ok.

    The catalog assumes 0.05 ATR slippage per fill (modeled). If the
    market drifts more than 2× that (0.10 ATR), the live trade no
    longer matches what the catalog certified.
    """
    # 1. Fetch tick
    try:
        resp = bridge_call("symbol_tick", {"symbol": symbol})
    except Exception as e:
        return PreflightResult(
            should_open=False,
            reason=f"bridge symbol_tick failed: {type(e).__name__}: {e}",
            current_bid=None, current_ask=None,
            signal_entry=signal_entry_price,
        )
    # Bridge returns either {"ok": True, "bid": x, "ask": y} or
    # nested under "data". Be tolerant.
    tick = resp.get("data", resp) if isinstance(resp, dict) else {}
    bid = tick.get("bid")
    ask = tick.get("ask")
    if bid is None or ask is None:
        return PreflightResult(
            should_open=False,
            reason=f"bridge symbol_tick returned no bid/ask: {resp}",
            current_bid=bid, current_ask=ask,
            signal_entry=signal_entry_price,
        )
    bid = float(bid)
    ask = float(ask)

    # 2. SL-already-breached check
    side_long = direction.upper() == "LONG"
    fill_price = ask if side_long else bid
    if side_long:
        if fill_price <= signal_stop_price:
            return PreflightResult(
                should_open=False,
                reason=(f"SL already breached — would fill LONG at {fill_price} "
                        f"≤ stop {signal_stop_price}"),
                current_bid=bid, current_ask=ask,
                signal_entry=signal_entry_price,
            )
    else:
        if fill_price >= signal_stop_price:
            return PreflightResult(
                should_open=False,
                reason=(f"SL already breached — would fill SHORT at {fill_price} "
                        f"≥ stop {signal_stop_price}"),
                current_bid=bid, current_ask=ask,
                signal_entry=signal_entry_price,
            )

    # 3. Drift-vs-ATR check
    drift = signal_entry_price - fill_price if side_long else fill_price - signal_entry_price
    drift_pips = drift / point_size if point_size > 0 else drift
    # Positive drift = market moved in our favor; only worry about adverse drift.
    adverse_drift = -drift   # positive when market moved against
    if atr_at_signal > 0:
        max_adverse = max_drift_atr_frac * atr_at_signal
        if adverse_drift > max_adverse:
            return PreflightResult(
                should_open=False,
                reason=(f"price drifted {adverse_drift:.6f} ({drift_pips:.1f} pips) "
                        f"against signal — exceeds {max_drift_atr_frac:.0%} ATR "
                        f"({max_adverse:.6f}); refusing"),
                current_bid=bid, current_ask=ask,
                signal_entry=signal_entry_price,
                drift_pips=drift_pips,
            )

    # 4. All checks pass
    return PreflightResult(
        should_open=True,
        reason="ok",
        current_bid=bid, current_ask=ask,
        signal_entry=signal_entry_price,
        drift_pips=drift_pips,
    )
