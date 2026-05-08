"""
strategy_resolver.py — single source of truth for variant→base mapping.

Optimizer variants encode params in the suffix (`ema_cross_9_20`,
`donchian_55`, `bbands_20_2`); the strategy registry stores ONE entry per
base class. Naive suffix-stripping isn't enough — `donchian_55` strips to
`donchian` but the registry has `donchian_breakout`. Use this resolver
everywhere a UI needs to look up the live class for an optimizer-named
variant.
"""
from __future__ import annotations

# Bases whose optimizer variant prefix differs from the registered class name.
_ALIASES: dict[str, str] = {
    "donchian":  "donchian_breakout",
    "rsi":       "rsi_meanrev",
    "bbands":    "bbands_meanrev",
    "first30":   "first30_meanrev",
}


def resolve_base_strategy(variant_name: str,
                          registered: dict) -> str | None:
    """Map an optimizer variant (e.g. `donchian_55`) to a registered
    base class name (e.g. `donchian_breakout`). Returns None if no
    match can be found."""
    # 1. Literal hit
    if variant_name in registered:
        return variant_name
    # 2. Progressive suffix-trim: 'ema_cross_9_20' → 'ema_cross_9' → 'ema_cross'
    for cut in range(2, 0, -1):
        candidate = "_".join(variant_name.split("_")[:-cut])
        if candidate and candidate in registered:
            return candidate
    # 3. Explicit aliases for renamed bases
    head = variant_name.split("_")[0]
    if head in _ALIASES and _ALIASES[head] in registered:
        return _ALIASES[head]
    return None
