"""
test_time_guards.py — INVARIANT-8 forced-flat schedule.

All times are UTC. The default config has us_session_close = "20:00",
flat_buffer_minutes = 5, no_entry_minutes_before_close = 30. So:
  - flat window:    [19:55, 20:00] UTC on weekdays
  - no-entry window: [19:30, 20:00] UTC on weekdays
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.time_guards import (
    TimeGuardCfg,
    in_no_entry_window,
    in_us_close_window,
    is_friday_close_window,
    needs_daily_flat,
    needs_weekend_flat,
    next_daily_flat_at,
    next_weekend_flat_at,
)


# A canonical config matching DEFAULT_CONFIG
DEFAULT_CFG = TimeGuardCfg(
    weekend_flat_all=True,
    daily_close_flat_classes=("stock", "index"),
    us_session_close_hhmm="20:00",
    flat_buffer_minutes=5,
    no_entry_minutes_before_close=30,
    asset_class_overrides={
        "stock":  ["META", "NVDA"],
        "index":  ["US100.cash", "US500.cash", "GER40.cash", "JP225"],
        "metal":  ["XAUUSD"],
        "energy": ["USOIL"],
        "fx":     [],
    },
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class TestCloseWindow:
    def test_inside_flat_window(self):
        # Tuesday 2026-05-05 at 19:55 UTC → in window
        assert in_us_close_window(utc(2026, 5, 5, 19, 55), "20:00", 5)
        assert in_us_close_window(utc(2026, 5, 5, 19, 58), "20:00", 5)
        assert in_us_close_window(utc(2026, 5, 5, 20, 0), "20:00", 5)

    def test_before_window(self):
        assert not in_us_close_window(utc(2026, 5, 5, 19, 54), "20:00", 5)
        assert not in_us_close_window(utc(2026, 5, 5, 14, 0), "20:00", 5)

    def test_after_window(self):
        assert not in_us_close_window(utc(2026, 5, 5, 20, 1), "20:00", 5)

    def test_weekend_returns_false(self):
        # Saturday + Sunday — no close window on weekends
        sat = utc(2026, 5, 9, 19, 55)   # Saturday
        sun = utc(2026, 5, 10, 19, 55)  # Sunday
        assert sat.weekday() == 5 and sun.weekday() == 6
        assert not in_us_close_window(sat, "20:00", 5)
        assert not in_us_close_window(sun, "20:00", 5)


class TestFridayWindow:
    def test_friday_in_close_window_returns_true(self):
        # 2026-05-08 is a Friday
        fri = utc(2026, 5, 8, 19, 55)
        assert fri.weekday() == 4
        assert is_friday_close_window(fri, "20:00", 5)

    def test_thursday_in_close_window_returns_false(self):
        thu = utc(2026, 5, 7, 19, 55)
        assert thu.weekday() == 3
        assert not is_friday_close_window(thu, "20:00", 5)

    def test_friday_outside_close_window_returns_false(self):
        fri = utc(2026, 5, 8, 12, 0)
        assert not is_friday_close_window(fri, "20:00", 5)


class TestNeedsWeekendFlat:
    def test_friday_close_triggers_flat(self):
        fri = utc(2026, 5, 8, 19, 55)
        assert needs_weekend_flat(fri, DEFAULT_CFG)

    def test_disabled_flag_blocks_flat(self):
        fri = utc(2026, 5, 8, 19, 55)
        cfg = TimeGuardCfg(
            weekend_flat_all=False,
            daily_close_flat_classes=DEFAULT_CFG.daily_close_flat_classes,
            us_session_close_hhmm=DEFAULT_CFG.us_session_close_hhmm,
            flat_buffer_minutes=DEFAULT_CFG.flat_buffer_minutes,
            no_entry_minutes_before_close=DEFAULT_CFG.no_entry_minutes_before_close,
            asset_class_overrides=DEFAULT_CFG.asset_class_overrides,
        )
        assert not needs_weekend_flat(fri, cfg)


class TestNeedsDailyFlat:
    def test_index_at_close_triggers(self):
        tue = utc(2026, 5, 5, 19, 55)
        assert needs_daily_flat("US100.cash", tue, DEFAULT_CFG)

    def test_stock_at_close_triggers(self):
        tue = utc(2026, 5, 5, 19, 55)
        assert needs_daily_flat("META", tue, DEFAULT_CFG)

    def test_fx_at_close_does_not_trigger(self):
        tue = utc(2026, 5, 5, 19, 55)
        assert not needs_daily_flat("EURUSD", tue, DEFAULT_CFG)

    def test_metal_at_close_does_not_trigger(self):
        tue = utc(2026, 5, 5, 19, 55)
        assert not needs_daily_flat("XAUUSD", tue, DEFAULT_CFG)

    def test_index_outside_close_window_does_not_trigger(self):
        tue = utc(2026, 5, 5, 14, 0)
        assert not needs_daily_flat("US100.cash", tue, DEFAULT_CFG)


class TestNoEntryWindow:
    def test_inside_no_entry_window(self):
        # Tuesday 19:31 UTC (1 min after 19:30)
        assert in_no_entry_window(utc(2026, 5, 5, 19, 31), DEFAULT_CFG)
        assert in_no_entry_window(utc(2026, 5, 5, 19, 50), DEFAULT_CFG)
        assert in_no_entry_window(utc(2026, 5, 5, 20, 0), DEFAULT_CFG)

    def test_outside_no_entry_window(self):
        # 19:29 — one minute before window starts
        assert not in_no_entry_window(utc(2026, 5, 5, 19, 29), DEFAULT_CFG)
        assert not in_no_entry_window(utc(2026, 5, 5, 14, 0), DEFAULT_CFG)
        assert not in_no_entry_window(utc(2026, 5, 5, 20, 1), DEFAULT_CFG)

    def test_weekend_no_entry_returns_false(self):
        sat = utc(2026, 5, 9, 19, 45)
        assert not in_no_entry_window(sat, DEFAULT_CFG)


class TestNextFlatAt:
    def test_next_weekend_flat_from_monday(self):
        # 2026-05-04 is Monday → next Friday is 2026-05-08
        mon = utc(2026, 5, 4, 12, 0)
        nxt = next_weekend_flat_at(mon, DEFAULT_CFG)
        assert nxt.weekday() == 4
        assert nxt.year == 2026 and nxt.month == 5 and nxt.day == 8
        assert nxt.hour == 19 and nxt.minute == 55

    def test_next_weekend_flat_from_friday_morning(self):
        # 2026-05-08 09:00 → next Friday-flat is THIS Friday at 19:55
        fri = utc(2026, 5, 8, 9, 0)
        nxt = next_weekend_flat_at(fri, DEFAULT_CFG)
        assert nxt.day == 8 and nxt.hour == 19 and nxt.minute == 55

    def test_next_weekend_flat_from_friday_after_flat(self):
        # 2026-05-08 19:56 — already past today's flat → next is 2026-05-15
        fri_late = utc(2026, 5, 8, 19, 56)
        nxt = next_weekend_flat_at(fri_late, DEFAULT_CFG)
        assert nxt.day == 15

    def test_next_daily_flat_from_tuesday(self):
        tue = utc(2026, 5, 5, 12, 0)
        nxt = next_daily_flat_at(tue, DEFAULT_CFG)
        # Tuesday 19:55 (same day)
        assert nxt.day == 5 and nxt.hour == 19 and nxt.minute == 55

    def test_next_daily_flat_skips_weekend(self):
        # 2026-05-08 (Friday) 19:56 → next weekday flat is Monday 2026-05-11
        fri_late = utc(2026, 5, 8, 19, 56)
        nxt = next_daily_flat_at(fri_late, DEFAULT_CFG)
        assert nxt.weekday() == 0   # Monday
        assert nxt.day == 11


class TestDSTBoundaryRobustness:
    """All math is in UTC, so the US DST flip cannot shift any of our
    decisions — but verify on the canonical fall-back day (2025-11-02 is
    the Sunday clocks fall back in the US in 2025)."""

    def test_dst_sunday_fallback_no_flats_due(self):
        sun = utc(2025, 11, 2, 19, 55)
        assert not needs_weekend_flat(sun, DEFAULT_CFG)
        assert not needs_daily_flat("US100.cash", sun, DEFAULT_CFG)
        assert not in_no_entry_window(sun, DEFAULT_CFG)

    def test_dst_following_monday_normal_flats(self):
        # 2025-11-03 Monday — normal weekday flats apply
        mon = utc(2025, 11, 3, 19, 55)
        assert mon.weekday() == 0
        assert needs_daily_flat("US100.cash", mon, DEFAULT_CFG)
        assert not needs_weekend_flat(mon, DEFAULT_CFG)
