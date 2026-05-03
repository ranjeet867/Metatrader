"""
asset_class.py — classify a broker symbol into one of:
    'stock' | 'index' | 'metal' | 'energy' | 'fx' | 'other'

Used by time_guards to decide whether daily-close-flat applies (stocks +
indices flat at US session close; FX, metals, energy hold overnight).

Resolution order:
  1. If `overrides` (from data/risk_config.json) lists the symbol under a
     class, return that class. Lookup is case-insensitive on the symbol
     SHORT NAME (broker suffixes like ".cash", ".pro", ".raw" are stripped
     before override matching, but '.cash' is also kept literal so both
     forms match).
  2. Heuristic: pattern-match the cleaned symbol.
       * exact alpha-6 (no digits, no dots) → 'fx'
       * starts with XAU/XAG/XPT → 'metal'
       * contains OIL/UKOIL/USOIL/BRENT/WTI/NGAS → 'energy'
       * contains an index magic-number (100/500/40/30/50/225) or
         tokens NDX/NAS/SPX/DAX/DOW/HSI/JP225 → 'index'
       * else → 'other'

Stocks always require an explicit override (no reliable heuristic).
"""
from __future__ import annotations

import re
from typing import Iterable, Literal


AssetClass = Literal["stock", "index", "metal", "energy", "fx", "other"]
VALID_CLASSES: tuple[AssetClass, ...] = (
    "stock", "index", "metal", "energy", "fx", "other"
)


_STRIP_SUFFIXES = (".cash", ".pro", ".raw", ".m", "+", "_", "-")


def _strip_suffix(symbol: str) -> str:
    s = symbol
    # First strip any of the broker-suffix tokens repeatedly. Don't iterate
    # forever — the stripper is single-pass per suffix from longest to shortest.
    for suf in sorted(_STRIP_SUFFIXES, key=len, reverse=True):
        if s.lower().endswith(suf):
            s = s[: -len(suf)]
    return s


_INDEX_TOKENS = re.compile(
    r"(?:^|[^A-Z])(NDX|NAS\d{0,3}|SPX|S&P|DAX|DOW|HSI|JP\d{2,3}|"
    r"FTSE|CAC|EU50|US30|US100|US500|GER40|JPX|NIKKEI)(?:[^A-Z]|$)",
    re.IGNORECASE,
)
_INDEX_NUMBER = re.compile(r"\b(?:100|500|40|30|50|225)\b")
_METAL_PREFIX = re.compile(r"^(XAU|XAG|XPT|XPD)", re.IGNORECASE)
_ENERGY_TOKENS = re.compile(
    r"(?:OIL|BRENT|WTI|NGAS|NATGAS|USOIL|UKOIL)", re.IGNORECASE,
)
_FX_PURE = re.compile(r"^[A-Z]{6}$")


def classify(symbol: str,
             overrides: dict[str, Iterable[str]] | None = None) -> AssetClass:
    """Map `symbol` to its AssetClass.

    Args:
        symbol: broker symbol (e.g. "US100.cash", "EURUSD", "XAUUSD").
        overrides: optional {class: [symbols, ...]} mapping. The first matching
                    class wins; classes are checked in the order given. The
                    intent is dashboards write this dict from
                    risk_config.json.asset_class_overrides.
    """
    sym = symbol.strip()
    if not sym:
        return "other"

    if overrides:
        sym_low = sym.lower()
        sym_strip_low = _strip_suffix(sym).lower()
        for cls in VALID_CLASSES:
            if cls not in overrides:
                continue
            for entry in overrides[cls]:
                e = entry.strip().lower()
                if e == sym_low or e == sym_strip_low:
                    return cls  # type: ignore[return-value]

    # Heuristic fallback (operates on the suffix-stripped form so that
    # "US100.cash" → "US100" → matches the index_number rule).
    bare = _strip_suffix(sym)

    if _METAL_PREFIX.match(bare):
        return "metal"
    if _ENERGY_TOKENS.search(bare):
        return "energy"
    if _INDEX_TOKENS.search(bare):
        return "index"
    # Tickers like "US100", "GER40" — index when alphanumeric with index numbers
    if re.search(r"[A-Z]+\d+", bare, re.IGNORECASE) and _INDEX_NUMBER.search(bare):
        return "index"
    if _FX_PURE.match(bare):
        return "fx"
    return "other"
