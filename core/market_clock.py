"""
market_clock.py — when is the market open for a given asset class.

We support four canonical asset-class schedules. Times are UTC.

  fx     — Sunday 22:00 → Friday 22:00. 24h between, no daily close.
  index  — weekday 14:30 → 21:00 UTC for US (US100/US500), or
           07:00 → 21:00 for Europe (GER40/EU50). Daily close between.
  metal  — Sunday 23:00 → Friday 22:00 (similar to FX but 1h gap).
  energy — Sunday 23:00 → Friday 22:00.

For the dashboard we don't need broker-exact accuracy — a small mistime
on the countdown is fine. What matters is "is the market open right now
for this strategy's instrument?" and "when does it next change state?".

Used by:
  - dashboards: render a green/red pill in the KPI strip
  - live_executor: refuse new entries when market is closed (defensive
    layer atop FTMO pre-close)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Literal


AssetClass = Literal["fx", "index_us", "index_eu", "metal", "energy", "stock"]


# Per-class weekly schedule. Each tuple = (open_dow, open_time, close_dow, close_time).
# DOW: Mon=0, Sun=6. All times UTC.
_SCHEDULES: dict[AssetClass, list[tuple[int, time, int, time]]] = {
    # FX: continuous Sunday 22:00 → Friday 22:00 UTC
    "fx":      [(6, time(22, 0), 4, time(22, 0))],
    # US indices: Mon-Fri 14:30 → 21:00 UTC (regular hours)
    "index_us": [(d, time(14, 30), d, time(21, 0)) for d in range(0, 5)],
    # European indices: Mon-Fri 07:00 → 21:00 UTC
    "index_eu": [(d, time(7, 0), d, time(21, 0)) for d in range(0, 5)],
    # Metals (XAUUSD): Sun 23:00 → Fri 22:00
    "metal":   [(6, time(23, 0), 4, time(22, 0))],
    # Energy (USOIL): Sun 23:00 → Fri 22:00
    "energy":  [(6, time(23, 0), 4, time(22, 0))],
    # US stocks: Mon-Fri 14:30 → 21:00
    "stock":   [(d, time(14, 30), d, time(21, 0)) for d in range(0, 5)],
}


@dataclass(frozen=True)
class MarketStatus:
    is_open: bool
    asset_class: AssetClass
    next_change_utc: datetime
    next_change_label: str   # "closes" | "opens"

    @property
    def seconds_until_change(self) -> float:
        return max(0.0,
                   (self.next_change_utc
                    - datetime.now(timezone.utc)).total_seconds())


# Simple ticker → asset class table. Extend as needed.
_TICKER_PATTERNS: list[tuple[str, AssetClass]] = [
    # Indices
    ("US100", "index_us"), ("US500", "index_us"), ("US30", "index_us"),
    ("NDX",  "index_us"), ("SPX",  "index_us"), ("DJI",  "index_us"),
    ("GER40", "index_eu"), ("EU50", "index_eu"), ("UK100", "index_eu"),
    ("FRA40", "index_eu"), ("DAX",  "index_eu"),
    # Metals / energies
    ("XAU", "metal"), ("XAG", "metal"), ("GOLD", "metal"),
    ("USOIL", "energy"), ("UKOIL", "energy"), ("BRENT", "energy"),
    ("WTI", "energy"),
    # Default to FX for currency pairs (must be after the more-specific patterns)
]


def classify(ticker: str) -> AssetClass:
    t = (ticker or "").upper()
    for needle, cls in _TICKER_PATTERNS:
        if needle in t:
            return cls
    # Heuristic for FX pairs: 6 letters of currency codes, possibly with
    # a separator like '.' for broker suffixes
    core = t.split(".")[0]
    if len(core) == 6 and core.isalpha():
        return "fx"
    return "fx"   # safe default — most FTMO instruments are FX-like


def _next_open_or_close(now_utc: datetime,
                        cls: AssetClass) -> tuple[bool, datetime, str]:
    """Walk forward up to 8 days looking for the next state transition."""
    schedule = _SCHEDULES.get(cls, _SCHEDULES["fx"])
    # Build a flat list of (start_dt, end_dt) windows for the next ~10 days
    base_monday = now_utc - timedelta(days=now_utc.weekday())
    base_monday = base_monday.replace(hour=0, minute=0, second=0,
                                       microsecond=0, tzinfo=timezone.utc)

    windows: list[tuple[datetime, datetime]] = []
    for week_offset in range(-1, 3):
        week_start = base_monday + timedelta(weeks=week_offset)
        for open_dow, open_t, close_dow, close_t in schedule:
            start = (week_start + timedelta(days=open_dow)
                      ).replace(hour=open_t.hour, minute=open_t.minute,
                                tzinfo=timezone.utc)
            close_offset = (close_dow - open_dow) % 7
            end = start + timedelta(days=close_offset)
            end = end.replace(hour=close_t.hour, minute=close_t.minute,
                                tzinfo=timezone.utc)
            if end <= start:
                end += timedelta(days=7)
            windows.append((start, end))
    windows.sort()

    # Are we in any window?
    for start, end in windows:
        if start <= now_utc < end:
            return (True, end, "closes")
    # Else: find the next start after now
    for start, end in windows:
        if start > now_utc:
            return (False, start, "opens")
    # Shouldn't happen — return a far-future placeholder
    return (False, now_utc + timedelta(days=7), "opens")


def status_for(ticker: str, *,
               now_utc: datetime | None = None) -> MarketStatus:
    """Convenience: classify + status."""
    n = now_utc or datetime.now(timezone.utc)
    cls = classify(ticker)
    is_open, change_at, label = _next_open_or_close(n, cls)
    return MarketStatus(is_open=is_open, asset_class=cls,
                          next_change_utc=change_at,
                          next_change_label=label)


def status_for_class(cls: AssetClass, *,
                      now_utc: datetime | None = None) -> MarketStatus:
    n = now_utc or datetime.now(timezone.utc)
    is_open, change_at, label = _next_open_or_close(n, cls)
    return MarketStatus(is_open=is_open, asset_class=cls,
                          next_change_utc=change_at,
                          next_change_label=label)
