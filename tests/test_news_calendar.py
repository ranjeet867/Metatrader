"""
test_news_calendar.py — verify event parsing, country→ticker mapping,
and blackout-window logic without making any HTTP requests.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import news_calendar as nc


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Path:
    return tmp_path / "news_calendar.json"


def _write_cache(path: Path, events: list[dict]) -> None:
    payload = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "events": events,
    }
    path.write_text(json.dumps(payload))


def test_event_country_to_instrument_mapping():
    e_usd = nc.NewsEvent(
        title="NFP", country="USD", impact="high",
        at_utc=datetime.now(timezone.utc),
    )
    assert e_usd.affects("EURUSD")          # USD pair
    assert e_usd.affects("US100.cash")      # US index
    assert e_usd.affects("XAUUSD")          # gold
    assert not e_usd.affects("JP225.cash")  # not USD-driven

    e_eur = nc.NewsEvent(
        title="ECB", country="EUR", impact="high",
        at_utc=datetime.now(timezone.utc),
    )
    assert e_eur.affects("EURUSD")
    assert e_eur.affects("GER40.cash")
    assert not e_eur.affects("AUDUSD")

    e_jpy = nc.NewsEvent(
        title="BoJ", country="JPY", impact="high",
        at_utc=datetime.now(timezone.utc),
    )
    assert e_jpy.affects("USDJPY")
    assert e_jpy.affects("JP225.cash")
    assert not e_jpy.affects("EURUSD")


def test_blackout_within_window(tmp_cache):
    event_time = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    _write_cache(tmp_cache, [
        {
            "title": "NFP", "country": "USD", "impact": "high",
            "at_utc": event_time.isoformat(),
        },
    ])
    # Signal 5 minutes before NFP → blackout
    sig_time = event_time - timedelta(minutes=5)
    b = nc.find_blackout(ticker="EURUSD", at_utc=sig_time,
                            cache_path=tmp_cache,
                            window_minutes=15)
    assert b is not None
    assert "NFP" in b.reason
    assert "USD" in b.reason


def test_no_blackout_outside_window(tmp_cache):
    event_time = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    _write_cache(tmp_cache, [
        {
            "title": "NFP", "country": "USD", "impact": "high",
            "at_utc": event_time.isoformat(),
        },
    ])
    # Signal 30 minutes before — outside the ±15min window
    sig_time = event_time - timedelta(minutes=30)
    b = nc.find_blackout(ticker="EURUSD", at_utc=sig_time,
                            cache_path=tmp_cache,
                            window_minutes=15)
    assert b is None


def test_no_blackout_for_non_matching_ticker(tmp_cache):
    event_time = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    _write_cache(tmp_cache, [
        {
            "title": "NFP", "country": "USD", "impact": "high",
            "at_utc": event_time.isoformat(),
        },
    ])
    sig_time = event_time
    # JP225 not affected by USD NFP
    b = nc.find_blackout(ticker="JP225.cash", at_utc=sig_time,
                            cache_path=tmp_cache,
                            window_minutes=15)
    assert b is None


def test_low_impact_events_dont_block(tmp_cache):
    event_time = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    _write_cache(tmp_cache, [
        {
            "title": "Some Speech", "country": "USD", "impact": "low",
            "at_utc": event_time.isoformat(),
        },
    ])
    sig_time = event_time
    # Default threshold is "high" → low impact NOT blocked
    b = nc.find_blackout(ticker="EURUSD", at_utc=sig_time,
                            cache_path=tmp_cache,
                            window_minutes=15)
    assert b is None


def test_medium_impact_blocked_when_threshold_lowered(tmp_cache):
    event_time = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    _write_cache(tmp_cache, [
        {
            "title": "Retail Sales", "country": "USD", "impact": "medium",
            "at_utc": event_time.isoformat(),
        },
    ])
    sig_time = event_time
    b_default = nc.find_blackout(ticker="EURUSD", at_utc=sig_time,
                                      cache_path=tmp_cache,
                                      window_minutes=15)
    assert b_default is None
    b_med = nc.find_blackout(ticker="EURUSD", at_utc=sig_time,
                                cache_path=tmp_cache,
                                window_minutes=15,
                                impact_threshold="medium")
    assert b_med is not None


def test_missing_cache_returns_none_not_crash(tmp_path):
    nonexistent = tmp_path / "missing.json"
    sig_time = datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    b = nc.find_blackout(ticker="EURUSD", at_utc=sig_time,
                            cache_path=nonexistent,
                            window_minutes=15)
    assert b is None


def test_load_cached_prunes_old_events(tmp_cache):
    old_event = (datetime.now(timezone.utc)
                 - timedelta(days=10)).isoformat()
    fresh_event = (datetime.now(timezone.utc)
                    + timedelta(hours=2)).isoformat()
    _write_cache(tmp_cache, [
        {"title": "Old", "country": "USD", "impact": "high",
         "at_utc": old_event},
        {"title": "Fresh", "country": "USD", "impact": "high",
         "at_utc": fresh_event},
    ])
    events = nc.load_cached(tmp_cache, auto_refresh=False)
    titles = [e.title for e in events]
    assert "Fresh" in titles
    assert "Old" not in titles


def test_upcoming_events_filters_by_horizon(tmp_cache):
    now = datetime.now(timezone.utc)
    in_2h = (now + timedelta(hours=2)).isoformat()
    in_48h = (now + timedelta(hours=48)).isoformat()
    _write_cache(tmp_cache, [
        {"title": "Soon", "country": "USD", "impact": "high",
         "at_utc": in_2h},
        {"title": "LaterEvent", "country": "USD", "impact": "high",
         "at_utc": in_48h},
    ])
    events = nc.upcoming_events_for_ticker(
        "EURUSD", horizon_hours=24, cache_path=tmp_cache)
    titles = [e.title for e in events]
    assert "Soon" in titles
    assert "LaterEvent" not in titles
