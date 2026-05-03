"""
test_ftmo_clock.py — INVARIANT-8 extension: pre-close window for FTMO daily reset.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from core.ftmo_clock import FtmoClock, FtmoRules


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_pre_close_window_for_indices_at_2155_utc():
    clk = FtmoClock(FtmoRules())
    # Tuesday 2026-05-05 21:55 UTC — exactly at the start of the window
    n = utc(2026, 5, 5, 21, 55)
    assert clk.is_within_pre_close_window(n, "index") is True


def test_pre_close_window_excludes_fx():
    clk = FtmoClock(FtmoRules())
    n = utc(2026, 5, 5, 21, 55)
    assert clk.is_within_pre_close_window(n, "fx") is False


def test_pre_close_window_outside_window():
    clk = FtmoClock(FtmoRules())
    # 21:54 — one min before window
    assert not clk.is_within_pre_close_window(utc(2026, 5, 5, 21, 54), "index")
    # 22:00 exactly — past the reset (window is [21:55, 22:00))
    assert not clk.is_within_pre_close_window(utc(2026, 5, 5, 22, 0), "index")
    # 22:01 — past
    assert not clk.is_within_pre_close_window(utc(2026, 5, 5, 22, 1), "index")


def test_pre_close_window_includes_metals_and_energy():
    clk = FtmoClock(FtmoRules())
    n = utc(2026, 5, 5, 21, 57)
    assert clk.is_within_pre_close_window(n, "metal") is True
    assert clk.is_within_pre_close_window(n, "energy") is True


def test_user_local_display_uses_user_tz_kolkata():
    """22:00 UTC → 03:30 IST. With 5-min pre-close → 03:25 IST."""
    rules = FtmoRules(user_tz=ZoneInfo("Asia/Kolkata"), pre_close_minutes=5)
    clk = FtmoClock(rules)
    n = utc(2026, 5, 5, 12, 0)   # noon UTC, well before today's reset
    nxt_local = clk.next_pre_close_user_local(n)
    assert str(nxt_local.tzinfo) == "Asia/Kolkata"
    assert nxt_local.hour == 3 and nxt_local.minute == 25
    # Date moves to the NEXT day in Kolkata (UTC 22:00 = 03:30 IST next day)
    assert nxt_local.day == 6


def test_next_reset_after_2200_advances_to_next_day():
    clk = FtmoClock(FtmoRules())
    # Already past reset → next is tomorrow
    n = utc(2026, 5, 5, 22, 30)
    nxt = clk.next_reset_utc(n)
    assert nxt.day == 6 and nxt.hour == 22


def test_time_until_pre_close_positive_and_decreasing():
    clk = FtmoClock(FtmoRules())
    a = clk.time_until_pre_close(utc(2026, 5, 5, 12, 0))
    b = clk.time_until_pre_close(utc(2026, 5, 5, 18, 0))
    assert a > b > timedelta(0)


def test_pre_close_skips_when_no_open_positions():
    """Just a window check — caller is responsible for the no-op when
    nothing is open. is_within_pre_close_window only inspects the clock."""
    clk = FtmoClock(FtmoRules())
    n = utc(2026, 5, 5, 21, 55)
    assert clk.is_within_pre_close_window(n, "index")
    # Caller's responsibility: skip work when no positions of that class
    # are open. This test documents the contract.


def test_custom_pre_close_minutes():
    rules = FtmoRules(pre_close_minutes=15)
    clk = FtmoClock(rules)
    # Window is now [21:45, 22:00)
    assert clk.is_within_pre_close_window(utc(2026, 5, 5, 21, 45), "index")
    assert clk.is_within_pre_close_window(utc(2026, 5, 5, 21, 50), "index")
    assert not clk.is_within_pre_close_window(utc(2026, 5, 5, 21, 44), "index")
