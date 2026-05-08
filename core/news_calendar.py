"""
news_calendar.py — fetch ForexFactory's red-folder calendar, store
locally, surface blackout windows around high-impact events.

Why this exists
---------------
Red-folder events (NFP, FOMC, CPI, GDP, BoE/ECB rate decisions) cause
sharp 30-50bps moves in the seconds after release. Mechanical
strategies that fire on the post-release whipsaw bar systematically
lose money — the reaction bar is dominated by HFT positioning, not
the same dynamics our backtest captured.

The fix is structural: BLOCK new opens within ±N minutes of any
red-folder event affecting the deployment's ticker.

Data source
-----------
ForexFactory provides a free JSON feed at:
  https://nfs.faireconomy.media/ff_calendar_thisweek.json

This is the official feed used by most retail apps. It's updated
hourly and includes:
  - title (event name)
  - country (USD / EUR / GBP / JPY / etc.)
  - date / time (UTC)
  - impact (high / medium / low) ← we filter to "high" only
  - actual / forecast / previous (post-release)

Caching
-------
Pulled once per day into data/news_calendar.json. Old events are
auto-pruned when older than 7 days. Blackout queries read from the
cached file — no live HTTP from the runner's tick path.

Affected-instrument mapping
---------------------------
We map FF country codes → tradeable instruments:
- USD events affect: USD pairs, US indices (US100, US30, US500), gold
- EUR events affect: EUR pairs, GER40, FRA40, EU50
- GBP events affect: GBP pairs, UK100
- JPY events affect: JPY pairs, JP225
- AUD events affect: AUD pairs
- CHF events affect: CHF pairs
- CAD events affect: CAD pairs, oil-correlated

Caller pattern
--------------
DeploymentRunner pre-flight check on every signal:

    from core import news_calendar
    blackout = news_calendar.find_blackout(
        db_path,  # data dir for cache
        ticker=d.ticker,
        at_utc=signal_time_utc,
        window_minutes=15,
    )
    if blackout is not None:
        rec.error = f"news blackout: {blackout.reason}"
        return  # skip this signal
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


FF_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
DEFAULT_CACHE_PATH = Path("data") / "news_calendar.json"
CACHE_MAX_AGE_HOURS = 12       # refresh feed if older than this
EVENT_PRUNE_AGE_DAYS = 7       # drop cached events older than this
DEFAULT_BLACKOUT_MINUTES = 15  # ±N minutes around event


# Map FF country codes → list of tradeable instrument prefixes that
# tend to react to that country's data. Conservative: prefer
# false-positive blackouts over false-negative whipsaws.
_COUNTRY_TO_INSTRUMENTS: dict[str, list[str]] = {
    "USD": ["USD", "US100", "US30", "US500", "US2000", "XAUUSD", "XAGUSD",
              "BTCUSD"],
    "EUR": ["EUR", "GER40", "FRA40", "EU50"],
    "GBP": ["GBP", "UK100"],
    "JPY": ["JPY", "JP225"],
    "AUD": ["AUD"],
    "CHF": ["CHF"],
    "CAD": ["CAD"],
    "NZD": ["NZD"],
    "CNY": ["HK50"],   # China data moves Hang Seng
    "ALL": [],         # All currencies / "all" affects everything
}


@dataclass
class NewsEvent:
    """One scheduled red-folder event."""
    title: str
    country: str
    impact: str              # "high" | "medium" | "low"
    at_utc: datetime
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None

    def affects(self, ticker: str) -> bool:
        """True if this event is likely to move `ticker`."""
        prefixes = _COUNTRY_TO_INSTRUMENTS.get(self.country, [])
        if not prefixes:
            return True   # "ALL" / unknown — treat as broad impact
        return any(ticker.upper().startswith(p)
                       or p in ticker.upper() for p in prefixes)


@dataclass
class Blackout:
    """A blackout window blocking new opens on a ticker."""
    ticker: str
    start_utc: datetime
    end_utc: datetime
    event_title: str
    event_country: str
    reason: str


def _parse_event(d: dict) -> Optional[NewsEvent]:
    """Parse one FF event dict. Returns None if malformed or
    too-low-impact."""
    try:
        impact = (d.get("impact") or "").lower()
        if impact not in ("high", "medium", "low"):
            return None
        # FF date format: "2026-05-08T13:30:00-05:00" (ET) typically
        date_str = d.get("date") or ""
        if not date_str:
            return None
        # Parse as ISO with offset
        if date_str.endswith("Z"):
            dt = datetime.fromisoformat(date_str[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(date_str)
        # Normalise to UTC
        dt_utc = dt.astimezone(timezone.utc)
        return NewsEvent(
            title=str(d.get("title") or ""),
            country=str(d.get("country") or "").upper(),
            impact=impact,
            at_utc=dt_utc,
            actual=d.get("actual") or None,
            forecast=d.get("forecast") or None,
            previous=d.get("previous") or None,
        )
    except (ValueError, KeyError, TypeError):
        return None


def fetch_feed(*, timeout_s: int = 10) -> list[NewsEvent]:
    """HTTP-fetch the FF feed and parse. Returns events; on any
    failure returns an empty list (and logs)."""
    try:
        req = Request(FF_FEED_URL, headers={
            "User-Agent": "mt5_quant_trader/1.0",
        })
        with urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        events = []
        for d in data:
            e = _parse_event(d)
            if e is not None:
                events.append(e)
        return events
    except Exception as exc:
        log.warning("news feed fetch failed: %s", exc)
        return []


def _events_to_dict(events: list[NewsEvent]) -> dict:
    return {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "events": [
            {
                "title": e.title, "country": e.country, "impact": e.impact,
                "at_utc": e.at_utc.isoformat(),
                "actual": e.actual, "forecast": e.forecast,
                "previous": e.previous,
            }
            for e in events
        ],
    }


def _events_from_dict(d: dict) -> list[NewsEvent]:
    out = []
    for ed in d.get("events", []):
        try:
            out.append(NewsEvent(
                title=ed["title"], country=ed["country"],
                impact=ed["impact"],
                at_utc=datetime.fromisoformat(ed["at_utc"]),
                actual=ed.get("actual"),
                forecast=ed.get("forecast"),
                previous=ed.get("previous"),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def refresh_cache(cache_path: Path = DEFAULT_CACHE_PATH) -> int:
    """Force-refresh the calendar cache. Returns # events fetched."""
    events = fetch_feed()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _events_to_dict(events)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(cache_path)
    return len(events)


def load_cached(cache_path: Path = DEFAULT_CACHE_PATH,
                  max_age_hours: int = CACHE_MAX_AGE_HOURS,
                  auto_refresh: bool = True) -> list[NewsEvent]:
    """Read events from cache. Auto-refreshes if stale and
    auto_refresh=True. Returns empty list if cache missing AND fetch
    failed."""
    needs_fetch = True
    if cache_path.exists():
        try:
            d = json.loads(cache_path.read_text())
            fetched = datetime.fromisoformat(d["fetched_at_utc"])
            age_h = (datetime.now(timezone.utc)
                     - fetched).total_seconds() / 3600
            needs_fetch = age_h > max_age_hours
            if not needs_fetch:
                events = _events_from_dict(d)
                # Prune events older than EVENT_PRUNE_AGE_DAYS
                cutoff = (datetime.now(timezone.utc)
                          - timedelta(days=EVENT_PRUNE_AGE_DAYS))
                return [e for e in events if e.at_utc >= cutoff]
        except (json.JSONDecodeError, KeyError, ValueError):
            needs_fetch = True

    if needs_fetch and auto_refresh:
        refresh_cache(cache_path)
        if cache_path.exists():
            try:
                d = json.loads(cache_path.read_text())
                return _events_from_dict(d)
            except Exception:
                return []
    return []


def find_blackout(
    *,
    ticker: str,
    at_utc: datetime,
    cache_path: Path = DEFAULT_CACHE_PATH,
    window_minutes: int = DEFAULT_BLACKOUT_MINUTES,
    impact_threshold: str = "high",
) -> Optional[Blackout]:
    """Return a Blackout if `at_utc` falls within a ±window_minutes
    band around any matching event. Otherwise None.

    `impact_threshold`: "high" (default) only blocks for red-folder.
    Set to "medium" to also block for orange-folder events.
    """
    events = load_cached(cache_path, auto_refresh=False)
    if not events:
        return None

    impacts_to_block = {"high"}
    if impact_threshold == "medium":
        impacts_to_block |= {"medium"}

    window = timedelta(minutes=window_minutes)
    for e in events:
        if e.impact not in impacts_to_block:
            continue
        if not e.affects(ticker):
            continue
        start = e.at_utc - window
        end = e.at_utc + window
        if start <= at_utc <= end:
            return Blackout(
                ticker=ticker,
                start_utc=start, end_utc=end,
                event_title=e.title,
                event_country=e.country,
                reason=(
                    f"{e.country} {e.title} ({e.impact}) at "
                    f"{e.at_utc.isoformat(timespec='minutes')}; "
                    f"blackout {start.isoformat(timespec='minutes')} → "
                    f"{end.isoformat(timespec='minutes')}"
                ),
            )
    return None


def upcoming_events_for_ticker(
    ticker: str,
    *,
    horizon_hours: int = 24,
    cache_path: Path = DEFAULT_CACHE_PATH,
    impact_threshold: str = "high",
) -> list[NewsEvent]:
    """Return events affecting `ticker` within the next `horizon_hours`.
    Used by Command Center / Operations to show "upcoming red-folder"
    strip."""
    events = load_cached(cache_path, auto_refresh=False)
    if not events:
        return []
    impacts = {"high"} if impact_threshold == "high" else {"high", "medium"}
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=horizon_hours)
    matching = [
        e for e in events
        if e.impact in impacts
        and e.affects(ticker)
        and now <= e.at_utc <= horizon
    ]
    matching.sort(key=lambda e: e.at_utc)
    return matching
