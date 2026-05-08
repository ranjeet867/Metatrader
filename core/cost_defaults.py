"""
cost_defaults.py — SINGLE SOURCE OF TRUTH for backtest cost assumptions.

Every place that runs a backtest in this repo MUST import these
constants instead of hard-coding values. This guarantees the same cell
shows the same metrics in the Backtest page, the Composer, the Strategy
Library, the Strategy Compare page, the sweep scripts, and run_backtest.py
CLI — so the user can read a number in one place and trust it elsewhere.

History: pre-this-module, the Backtest page used $3 + 0.10×ATR while
the sweep tool used $4 + 0.05×ATR. A cell scored 23 in the sweep but
PF<1 when re-run interactively, because the user was unwittingly
comparing two different cost configs. Bug-class avoided here.

Tweaking the production cost model:
  1. Edit the constants below.
  2. Run scripts/rebaseline_catalog.py to re-run every catalog cell.
  3. v2.db now contains a self-consistent set of metrics. Composer +
     Library + Backtest page all read those metrics, all aligned.
"""
from __future__ import annotations


# ── Cost constants ────────────────────────────────────────────────────
# These match the production friction the user actually experiences on
# a typical FTMO-style account: $4 round-trip commission per trade
# (covers spread + broker fee), and 0.05 ATR slippage per fill (covers
# adverse fill on entry + exit). Lift either value if your broker is
# more expensive.

DEFAULT_COMMISSION_USD = 4.0
"""Round-trip commission per trade in USD. Charged once per closed
trade (the engine deducts $4 on close). Covers spread + broker fee."""

DEFAULT_SLIPPAGE_ATR_FRAC = 0.05
"""Slippage per fill as a fraction of the bar's ATR. Applied AGAINST
the trade at both entry and exit, so total round-trip cost is
2 × slippage_atr_frac × ATR. 0.05 = ~5% of ATR adverse on each fill."""

DEFAULT_STARTING_BALANCE_USD = 100_000.0
"""Starting equity for backtests. Aligned with FTMO standard $100k
challenge accounts so $-figures translate directly to challenge P&L."""

DEFAULT_TRAIN_PCT = 0.6
"""Fraction of bars used for in-sample training. The remaining 40%
is the OOS test partition that the hard gates evaluate."""


# ── Sizing defaults ───────────────────────────────────────────────────
# Used when the caller doesn't pass an explicit risk_pct or lots.

DEFAULT_RISK_PCT = 0.30
"""Risk-% per trade for dynamic sizing. 0.30% of equity = $300 at
$100k balance. Composer's auto-tuner can shrink this further."""


# ── Display helper ────────────────────────────────────────────────────

def cost_config_badge() -> str:
    """One-line human-readable summary for UI display.

    Used by Composer / Library / Backtest page captions so the user
    can see at a glance which cost model produced the displayed
    metrics — and confirm every view is on the same benchmark."""
    return (
        f"${DEFAULT_COMMISSION_USD:.0f} commission · "
        f"{DEFAULT_SLIPPAGE_ATR_FRAC:.2f}×ATR slip · "
        f"${DEFAULT_STARTING_BALANCE_USD:,.0f} bal · "
        f"{int(DEFAULT_TRAIN_PCT*100)}/{int((1-DEFAULT_TRAIN_PCT)*100)} "
        f"train/test"
    )


def as_dict() -> dict:
    """Snapshot of all defaults, for storage alongside backtest runs
    so we can detect later if a cached metric was produced under a
    different cost regime."""
    return {
        "commission_usd": DEFAULT_COMMISSION_USD,
        "slippage_atr_frac": DEFAULT_SLIPPAGE_ATR_FRAC,
        "starting_balance_usd": DEFAULT_STARTING_BALANCE_USD,
        "train_pct": DEFAULT_TRAIN_PCT,
        "risk_pct": DEFAULT_RISK_PCT,
    }
