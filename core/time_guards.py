"""
time_guards.py — INVARIANT-8: time-based forced exits.

The two non-overridable rules:
  (a) WEEKEND-FLAT — every Friday at (us_session_close - flat_buffer_minutes) UTC,
      ALL open positions are forced-closed regardless of asset class.
  (b) DAILY-CLOSE-FLAT — every weekday at the same time, all positions in
      symbols classified as 'stock' or 'index' (configurable) are forced-closed.

A separate, related concept: the NO-ENTRY WINDOW. In the last
`no_entry_minutes_before_close` minutes of every weekday session we BLOCK new
entries — the goal is to avoid opening trades that would be force-flatted
within minutes.

All math is in UTC. The config stores `us_session_close_utc` as "HH:MM" — when
US clocks change for DST, the user must update that string. We never silently
shift it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable

from core.asset_class import classify


@dataclass(frozen=True)
class TimeGuardCfg:
    """Subset of risk_config used by time_guards. Decouples from the full
    RiskConfig — the executor / runner / paper_loop pass this in.

    The `enforce_daily_flat` flag mirrors `run_backtest`'s opt-in for
    daily-close behaviour. Pre-fix paper_executor would fire daily-flat
    whenever `daily_close_flat_classes` was non-empty, but `run_backtest`
    only fired daily-flat when its `enforce_daily_flat` arg was True.
    Replay using TimeGuardCfg directly could thus DIVERGE from backtest
    on stocks/indices when a caller set `weekend_flat_all=True` without
    intending daily-flat behaviour. Now both engines gate on this flag.

    Default True preserves existing live/paper behaviour. Replay paths
    that build TimeGuardCfg from a backtest's flag should pass the
    actual enforce_daily_flat value through (or use
    `time_guard_cfg_from_risk_config(... enforce_daily_flat=...)`).
    """
    weekend_flat_all: bool
    daily_close_flat_classes: tuple[str, ...]
    us_session_close_hhmm: str            # "HH:MM" UTC
    flat_buffer_minutes: int
    no_entry_minutes_before_close: int
    asset_class_overrides: dict[str, Iterable[str]] | None = None
    enforce_daily_flat: bool = True       # NEW — gate daily-flat firing


def _parse_hhmm(s: str) -> time:
    """Convert "HH:MM" string to a datetime.time object. Raises on bad input."""
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected 'HH:MM', got {s!r}")
    h, m = int(parts[0]), int(parts[1])
    return time(h, m)


def _utc(dt: datetime) -> datetime:
    """Coerce a datetime to tz-aware UTC. Naive datetimes are assumed UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _close_dt_today(when_utc: datetime, hhmm: str) -> datetime:
    """The session-close datetime for the calendar date of `when_utc`."""
    when = _utc(when_utc)
    t = _parse_hhmm(hhmm)
    return datetime(when.year, when.month, when.day, t.hour, t.minute,
                     tzinfo=timezone.utc)


def in_us_close_window(when_utc: datetime, close_hhmm: str,
                       buffer_min: int) -> bool:
    """True iff `when_utc` falls in [close - buffer, close] on a weekday.

    Weekend (Sat/Sun) always returns False — no daily-close on weekends.
    """
    when = _utc(when_utc)
    if when.weekday() >= 5:           # 5=Sat, 6=Sun
        return False
    close_dt = _close_dt_today(when, close_hhmm)
    start_dt = close_dt - timedelta(minutes=int(buffer_min))
    return start_dt <= when <= close_dt


def is_friday_close_window(when_utc: datetime, close_hhmm: str,
                            buffer_min: int) -> bool:
    """True iff in_us_close_window AND the day is Friday."""
    when = _utc(when_utc)
    return when.weekday() == 4 and in_us_close_window(when, close_hhmm, buffer_min)


def needs_weekend_flat(when_utc: datetime, cfg: TimeGuardCfg) -> bool:
    """Friday close-window AND cfg.weekend_flat_all is enabled."""
    if not cfg.weekend_flat_all:
        return False
    return is_friday_close_window(when_utc, cfg.us_session_close_hhmm,
                                  cfg.flat_buffer_minutes)


def needs_daily_flat(symbol: str, when_utc: datetime, cfg: TimeGuardCfg) -> bool:
    """In US close-window AND symbol's class is in daily_close_flat_classes.

    Note: weekend_flat takes precedence — if both fire, weekend_flat wins
    (callers should test needs_weekend_flat first; this function still
    returns True when applicable independent of weekend status, since the
    forced-close action itself is the same — only the audit `reason` differs).
    """
    cls = classify(symbol, overrides=cfg.asset_class_overrides)
    if cls not in cfg.daily_close_flat_classes:
        return False
    return in_us_close_window(when_utc, cfg.us_session_close_hhmm,
                              cfg.flat_buffer_minutes)


def in_no_entry_window(when_utc: datetime, cfg: TimeGuardCfg) -> bool:
    """True iff `when_utc` is in [close - no_entry_minutes, close] on a weekday.

    Used to BLOCK new entries — separate from the (smaller) flat-buffer
    window that triggers forced-close.
    """
    when = _utc(when_utc)
    if when.weekday() >= 5:
        return False
    close_dt = _close_dt_today(when, cfg.us_session_close_hhmm)
    start_dt = close_dt - timedelta(minutes=cfg.no_entry_minutes_before_close)
    return start_dt <= when <= close_dt


def next_weekend_flat_at(now_utc: datetime, cfg: TimeGuardCfg) -> datetime:
    """Return the next Friday-close-flat datetime AFTER `now_utc`."""
    now = _utc(now_utc)
    t = _parse_hhmm(cfg.us_session_close_hhmm)
    flat_offset = timedelta(minutes=cfg.flat_buffer_minutes)
    # Step day-by-day; loop bounded by 7 — always finds Friday within a week
    for delta in range(0, 8):
        candidate_date = (now + timedelta(days=delta)).date()
        candidate = datetime(candidate_date.year, candidate_date.month,
                              candidate_date.day, t.hour, t.minute,
                              tzinfo=timezone.utc) - flat_offset
        if candidate.weekday() == 4 and candidate > now:
            return candidate
    raise RuntimeError("unreachable: no Friday found within 7 days")


def next_daily_flat_at(now_utc: datetime, cfg: TimeGuardCfg) -> datetime:
    """Return the next weekday-close-flat datetime AFTER `now_utc`.

    Same as next_weekend_flat_at but matches Mon-Fri. If `now_utc` is exactly
    AT today's flat-time, returns the NEXT weekday's flat-time (strict >).
    """
    now = _utc(now_utc)
    t = _parse_hhmm(cfg.us_session_close_hhmm)
    flat_offset = timedelta(minutes=cfg.flat_buffer_minutes)
    for delta in range(0, 8):
        candidate_date = (now + timedelta(days=delta)).date()
        candidate = datetime(candidate_date.year, candidate_date.month,
                              candidate_date.day, t.hour, t.minute,
                              tzinfo=timezone.utc) - flat_offset
        if candidate.weekday() < 5 and candidate > now:
            return candidate
    raise RuntimeError("unreachable: no weekday found within 7 days")


def time_guard_cfg_from_risk_config(cfg) -> TimeGuardCfg:
    """Convenience: build a TimeGuardCfg from a core.config.RiskConfig instance."""
    return TimeGuardCfg(
        weekend_flat_all=cfg.weekend_flat_all,
        daily_close_flat_classes=cfg.daily_close_flat_classes,
        us_session_close_hhmm=cfg.us_session_close_utc,
        flat_buffer_minutes=cfg.flat_buffer_minutes,
        no_entry_minutes_before_close=cfg.no_entry_minutes_before_close,
        asset_class_overrides=cfg.asset_class_overrides,
    )
