"""
test_market_clock.py — instrument classification + open/closed state +
next-transition timestamp for the dashboard's market-status pill.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import market_clock as mc


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker, expected", [
    ("US100.cash", "index_us"),
    ("US500.cash", "index_us"),
    ("GER40.cash", "index_eu"),
    ("EU50.cash",  "index_eu"),
    ("XAUUSD",     "metal"),
    ("XAGUSD",     "metal"),
    ("USOIL",      "energy"),
    ("EURUSD",     "fx"),
    ("USDJPY",     "fx"),
    ("GBPJPY",     "fx"),
    ("AUDUSD",     "fx"),
    ("UNKNOWN",    "fx"),    # safe default
])
def test_classify(ticker, expected):
    assert mc.classify(ticker) == expected


# ---------------------------------------------------------------------------
# FX schedule — Sun 22:00 UTC → Fri 22:00 UTC
# ---------------------------------------------------------------------------

def test_fx_open_during_weekday():
    # Tuesday 12:00 UTC — middle of FX week
    when = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    s = mc.status_for("EURUSD", now_utc=when)
    assert s.is_open
    assert s.next_change_label == "closes"
    # Closes Friday 22:00 UTC of the same week
    expected_close = datetime(2026, 5, 8, 22, 0, tzinfo=timezone.utc)
    assert s.next_change_utc == expected_close


def test_fx_closed_on_saturday():
    when = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    s = mc.status_for("EURUSD", now_utc=when)
    assert not s.is_open
    assert s.next_change_label == "opens"
    # Opens Sunday 22:00 UTC
    expected_open = datetime(2026, 5, 10, 22, 0, tzinfo=timezone.utc)
    assert s.next_change_utc == expected_open


def test_fx_closed_just_after_friday_close():
    # Friday 22:30 UTC — market closed
    when = datetime(2026, 5, 8, 22, 30, tzinfo=timezone.utc)
    s = mc.status_for("EURUSD", now_utc=when)
    assert not s.is_open


def test_fx_open_just_after_sunday_open():
    # Sunday 22:30 UTC — market open
    when = datetime(2026, 5, 10, 22, 30, tzinfo=timezone.utc)
    s = mc.status_for("EURUSD", now_utc=when)
    assert s.is_open


# ---------------------------------------------------------------------------
# US index schedule — Mon-Fri 14:30 → 21:00 UTC
# ---------------------------------------------------------------------------

def test_us_index_closed_pre_open():
    # Tuesday 09:00 UTC — before US open
    when = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
    s = mc.status_for("US100.cash", now_utc=when)
    assert not s.is_open
    assert s.next_change_label == "opens"
    # Opens at 14:30 same day
    assert s.next_change_utc == datetime(2026, 5, 5, 14, 30,
                                            tzinfo=timezone.utc)


def test_us_index_open_during_session():
    when = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    s = mc.status_for("US100.cash", now_utc=when)
    assert s.is_open


def test_us_index_closed_overnight():
    # Tuesday 22:00 — past close
    when = datetime(2026, 5, 5, 22, 0, tzinfo=timezone.utc)
    s = mc.status_for("US100.cash", now_utc=when)
    assert not s.is_open
    # Opens Wednesday 14:30
    assert s.next_change_utc == datetime(2026, 5, 6, 14, 30,
                                            tzinfo=timezone.utc)


def test_us_index_closed_weekend():
    # Saturday
    when = datetime(2026, 5, 9, 18, 0, tzinfo=timezone.utc)
    s = mc.status_for("US500.cash", now_utc=when)
    assert not s.is_open
    # Opens Monday 14:30
    assert s.next_change_utc == datetime(2026, 5, 11, 14, 30,
                                            tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# EU index — opens earlier (07:00 UTC)
# ---------------------------------------------------------------------------

def test_eu_index_open_before_us_open():
    # Tuesday 09:00 UTC — DAX open, ES/NDX still closed
    when = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
    assert mc.status_for("GER40.cash", now_utc=when).is_open
    assert not mc.status_for("US100.cash", now_utc=when).is_open


# ---------------------------------------------------------------------------
# seconds_until_change
# ---------------------------------------------------------------------------

def test_seconds_until_change_is_positive():
    when = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    s = mc.status_for("EURUSD", now_utc=when)
    # Note: seconds_until_change uses datetime.now() so we can't assert
    # exact value here. Only check it's a non-negative number.
    assert s.seconds_until_change >= 0


# ---------------------------------------------------------------------------
# status_for_class direct call
# ---------------------------------------------------------------------------

def test_status_for_class_direct():
    when = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    s = mc.status_for_class("metal", now_utc=when)
    assert s.is_open
    assert s.asset_class == "metal"


def test_status_for_class_metal_closes_friday_22():
    when = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    s = mc.status_for_class("metal", now_utc=when)
    assert s.is_open
    assert s.next_change_utc == datetime(2026, 5, 8, 22, 0,
                                            tzinfo=timezone.utc)
