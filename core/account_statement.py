"""
account_statement.py — broker-truth daily / total PnL view.

Pulls realized PnL from history_deals_get (broker side), so even if our
process was offline for hours we still see what really happened. Combines
with positions_get for unrealized PnL.

The "today" boundary is the FTMO daily reset (22:00 UTC), NOT the user's
local midnight — the user is FTMO-compliant when balance hasn't dropped
>5% since 22:00 UTC. The user-local timezone is used only for display.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from core.ftmo_clock import FtmoClock, FtmoRules
from core.mt5_account import BridgeError, MT5AccountClient


@dataclass(frozen=True)
class StatementSummary:
    account_login: int
    starting_equity: float            # baseline at FTMO challenge start
    current_equity: float
    current_balance: float
    realized_pnl_today: float          # since last 22:00 UTC reset
    realized_pnl_total: float          # since starting_equity baseline
    unrealized_pnl: float
    n_trades_today: int
    n_trades_total: int
    daily_loss_remaining: float        # FTMO daily cap minus today's loss
    total_loss_remaining: float        # FTMO total cap minus all-time loss
    last_deal_at_utc: str | None


class StatementReader:
    """Composes account_info + positions_get + history_deals_get into a
    single point-in-time picture the dashboard renders."""

    def __init__(self, *, account_login: int, bridge: MT5AccountClient,
                 ftmo_rules: FtmoRules, account_baseline: float,
                 daily_loss_cap_pct: float = 5.0,
                 total_loss_cap_pct: float = 10.0,
                 history_lookback_days: int = 60):
        self.account_login = int(account_login)
        self.bridge = bridge
        self.ftmo_rules = ftmo_rules
        self.account_baseline = float(account_baseline)
        self.daily_loss_cap_pct = float(daily_loss_cap_pct)
        self.total_loss_cap_pct = float(total_loss_cap_pct)
        self.history_lookback_days = int(history_lookback_days)
        self._clock = FtmoClock(ftmo_rules)

    def snapshot(self, *, now_utc: datetime | None = None) -> StatementSummary:
        now = now_utc or datetime.now(timezone.utc)
        since = now - timedelta(days=self.history_lookback_days)

        # Account info (caches under bridge.ttl_seconds)
        ai = self.bridge.account_info()
        equity = ai.equity
        balance = ai.balance

        # Open positions → unrealized
        try:
            positions = self.bridge.positions_get()
        except BridgeError:
            positions = []
        unrealized = sum(p.profit + p.swap + p.commission for p in positions)

        # History → realized split
        try:
            deals = self.bridge.history_deals_get(since_utc=since)
        except BridgeError:
            deals = []

        # The "today" boundary is the most recent reset before `now`.
        today_boundary_utc = self._most_recent_reset_before(now)
        realized_total = 0.0
        realized_today = 0.0
        n_trades_total = 0
        n_trades_today = 0
        last_deal_at = None
        for d in deals:
            # Count only OUT deals (closes) for n_trades; profit on closes is
            # the realized PnL for that round-trip.
            if d.entry != 1:
                continue
            ts_iso = d.time_utc
            try:
                ts = datetime.fromisoformat(ts_iso)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            pnl = float(d.profit + d.swap + d.commission)
            realized_total += pnl
            n_trades_total += 1
            if ts >= today_boundary_utc:
                realized_today += pnl
                n_trades_today += 1
            if last_deal_at is None or ts > last_deal_at:
                last_deal_at = ts

        # Cap headroom
        daily_cap_dollars = self.account_baseline * self.daily_loss_cap_pct / 100.0
        total_cap_dollars = self.account_baseline * self.total_loss_cap_pct / 100.0
        # Today's loss is the negative portion of realized_today + unrealized
        # if the position is currently underwater. Conservatively, FTMO
        # measures daily loss against the day_start_balance.
        day_loss = max(0.0, -(realized_today + unrealized))
        daily_loss_remaining = max(0.0, daily_cap_dollars - day_loss)

        all_time_loss = max(0.0, self.account_baseline - equity)
        total_loss_remaining = max(0.0, total_cap_dollars - all_time_loss)

        return StatementSummary(
            account_login=self.account_login,
            starting_equity=self.account_baseline,
            current_equity=equity, current_balance=balance,
            realized_pnl_today=realized_today,
            realized_pnl_total=realized_total,
            unrealized_pnl=unrealized,
            n_trades_today=n_trades_today,
            n_trades_total=n_trades_total,
            daily_loss_remaining=daily_loss_remaining,
            total_loss_remaining=total_loss_remaining,
            last_deal_at_utc=last_deal_at.isoformat() if last_deal_at else None,
        )

    def daily_pnl_curve(self, days: int = 30,
                         *, now_utc: datetime | None = None) -> pd.DataFrame:
        """Per-day realized PnL, derived from broker history. The day
        boundaries are FTMO resets (22:00 UTC), NOT calendar midnight."""
        now = now_utc or datetime.now(timezone.utc)
        since = now - timedelta(days=days)
        try:
            deals = self.bridge.history_deals_get(since_utc=since)
        except BridgeError:
            return pd.DataFrame(columns=["day_start_utc", "realized_pnl",
                                            "n_trades"])
        # Bucket by reset day
        buckets: dict[str, dict] = {}
        for d in deals:
            if d.entry != 1:
                continue
            try:
                ts = datetime.fromisoformat(d.time_utc)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            day_start = self._most_recent_reset_before(ts)
            key = day_start.isoformat()
            b = buckets.setdefault(key, {
                "day_start_utc": day_start,
                "realized_pnl": 0.0, "n_trades": 0,
            })
            b["realized_pnl"] += float(d.profit + d.swap + d.commission)
            b["n_trades"] += 1
        rows = sorted(buckets.values(), key=lambda r: r["day_start_utc"])
        return pd.DataFrame(rows)

    # --- helpers --------------------------------------------------------

    def _most_recent_reset_before(self, when_utc: datetime) -> datetime:
        """Most recent FTMO reset (22:00 UTC) AT OR BEFORE `when_utc`."""
        n = when_utc if when_utc.tzinfo else when_utc.replace(tzinfo=timezone.utc)
        cand = datetime(
            n.year, n.month, n.day,
            self.ftmo_rules.daily_reset_utc.hour,
            self.ftmo_rules.daily_reset_utc.minute,
            tzinfo=timezone.utc,
        )
        if cand > n:
            cand -= timedelta(days=1)
        return cand
