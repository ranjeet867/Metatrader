"""
ftmo_clock.py — timezone-aware FTMO daily-reset awareness.

FTMO's daily-loss window resets at 22:00 UTC. We force-flat cash / CFD / index
positions a few minutes BEFORE the reset because:
  - intra-bar slippage at the reset boundary can wipe an account
  - some brokers freeze symbols during the maintenance gap
  - FX is usually 24-hour and can stay open

The same clock drives the dashboard's "next FTMO close" countdown shown in
the user's local timezone (e.g. 03:55 IST in Asia/Kolkata).

INVARIANT-8 already implements weekend-flat / daily-close-flat at 19:55 UTC
(US session close). The FTMO pre-close at ~21:55 UTC is a SEPARATE event —
both can apply on the same day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class FtmoRules:
    """Configurable rules for FTMO close behaviour. Default values match
    the FTMO Phase 1 spec at the time of build (2026-05)."""
    daily_reset_utc: time = field(default_factory=lambda: time(22, 0))
    pre_close_minutes: int = 5
    user_tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    classes_requiring_pre_close: frozenset[str] = frozenset(
        {"stock", "index", "metal", "energy"}
    )


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FtmoClock:
    """Pure-function helpers around an FtmoRules instance."""

    def __init__(self, rules: FtmoRules | None = None):
        self.rules = rules or FtmoRules()

    # ---- next pre-close calculations ----

    def next_reset_utc(self, now_utc: datetime) -> datetime:
        """The next 22:00 UTC instant strictly after `now_utc`."""
        n = _utc(now_utc)
        candidate = datetime(
            n.year, n.month, n.day,
            self.rules.daily_reset_utc.hour,
            self.rules.daily_reset_utc.minute,
            tzinfo=timezone.utc,
        )
        if candidate <= n:
            candidate += timedelta(days=1)
        return candidate

    def next_pre_close_utc(self, now_utc: datetime) -> datetime:
        """Next pre-close instant (= next_reset_utc - pre_close_minutes)."""
        return self.next_reset_utc(now_utc) - timedelta(
            minutes=self.rules.pre_close_minutes
        )

    def next_pre_close_user_local(self, now_utc: datetime) -> datetime:
        """The next pre-close instant projected into the user's local tz.

        Used purely for display ("Next FTMO close: 03:55 IST"). All
        decision math runs in UTC.
        """
        return self.next_pre_close_utc(now_utc).astimezone(self.rules.user_tz)

    # ---- decision helpers ----

    def is_within_pre_close_window(self, now_utc: datetime,
                                    asset_class: str) -> bool:
        """True iff `now_utc` is in [reset - pre_close_minutes, reset)
        AND the asset_class is one we close pre-reset.

        Live executor refuses entries when this is True. The paper / live
        loops force-close existing positions of the same asset class.
        """
        if asset_class not in self.rules.classes_requiring_pre_close:
            return False
        n = _utc(now_utc)
        # Compute the reset for `now`'s calendar day in UTC. The window is
        # [reset - pre_close_minutes, reset).
        reset_today = datetime(
            n.year, n.month, n.day,
            self.rules.daily_reset_utc.hour,
            self.rules.daily_reset_utc.minute,
            tzinfo=timezone.utc,
        )
        window_start = reset_today - timedelta(minutes=self.rules.pre_close_minutes)
        return window_start <= n < reset_today

    def time_until_pre_close(self, now_utc: datetime) -> timedelta:
        """Wall-clock remaining until the next pre-close fires."""
        return self.next_pre_close_utc(now_utc) - _utc(now_utc)
