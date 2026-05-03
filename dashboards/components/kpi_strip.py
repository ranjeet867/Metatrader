"""
kpi_strip.py — full-width header band with the 8 numbers a trader cares
about: balance, equity, unrealized, today realized, daily-loss buffer,
total-loss buffer, profit progress, days-traded.

All values use a monospace tabular-numerals style so columns stay aligned
even when balances move. Color rules:
  green   > 0
  red     < 0
  amber   between 60-80% of an FTMO cap
  dark    no data
"""
from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from core import account_manager
from core.account_statement import StatementSummary
from core.ftmo_clock import FtmoClock


def _humanize(td_seconds: float) -> str:
    s = max(0, int(td_seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, mm = divmod(m, 60)
    if h < 24:
        return f"{h}h{mm:02d}m"
    d, hh = divmod(h, 24)
    return f"{d}d{hh}h"


def _color_for_buffer(used_pct: float) -> str:
    if used_pct >= 80:
        return "#dc2626"   # red
    if used_pct >= 60:
        return "#f59e0b"   # amber
    return "#16a34a"       # green


def _money(v: float, sign: bool = False) -> str:
    fmt = "${:+,.2f}" if sign else "${:,.2f}"
    return fmt.format(v)


def _kpi(label: str, value: str, *, color: str = "#e5e7eb",
         sub: str | None = None) -> str:
    sub_html = (f"<div style='color:#9ca3af;font-size:0.72rem;"
                f"font-family:ui-monospace,Menlo,monospace;"
                f"letter-spacing:0.02em;'>{sub}</div>") if sub else ""
    return (
        f"<div style='background:#0f1419;border:1px solid #1f2937;"
        f"border-radius:8px;padding:10px 14px;height:78px;"
        f"display:flex;flex-direction:column;justify-content:center;'>"
        f"<div style='color:#9ca3af;font-size:0.70rem;text-transform:uppercase;"
        f"letter-spacing:0.08em;margin-bottom:2px;'>{label}</div>"
        f"<div style='color:{color};font-size:1.30rem;font-weight:600;"
        f"font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums;"
        f"line-height:1.1;'>{value}</div>"
        f"{sub_html}"
        f"</div>"
    )


def render(*, account: "account_manager.Account",
           summary: StatementSummary | None,
           clock: FtmoClock,
           container=None) -> None:
    target = container or st

    baseline = account.effective_baseline_equity
    daily_cap_pct = account.daily_loss_cap_pct
    total_cap_pct = account.total_loss_cap_pct
    profit_target_pct = account.profit_target_pct
    days_required = account.days_required

    daily_cap_dollars = baseline * daily_cap_pct / 100.0
    total_cap_dollars = baseline * total_cap_pct / 100.0
    profit_target_dollars = baseline * profit_target_pct / 100.0

    # Defaults if bridge offline
    bal = eq = unreal = today_real = 0.0
    days_traded = 0
    next_close_local = None
    next_close_eta_s = 0.0

    now = datetime.now(timezone.utc)
    next_close_utc = clock.next_pre_close_utc(now)
    next_close_eta_s = (next_close_utc - now).total_seconds()
    next_close_local = clock.next_pre_close_user_local(now)

    if summary is not None:
        bal = summary.current_balance
        eq = summary.current_equity
        unreal = summary.unrealized_pnl
        today_real = summary.realized_pnl_today
        days_traded = max(0, getattr(summary, "n_days_traded", 0) or 0)

    # FTMO buffer math.
    #
    # Daily-loss buffer is measured against the user's ANCHOR (their
    # `risk_baseline_equity`) — that's the day-start balance for sizing
    # purposes, and FTMO's daily check resets every 22:00 UTC so the
    # anchor is the right reference.
    #
    # Total-loss buffer is FUNDAMENTALLY different: FTMO measures it
    # against the ORIGINAL challenge starting balance (challenge_100k →
    # $100k), and that floor never moves regardless of how the operator
    # personally re-anchors. So total-loss math here ALWAYS uses the
    # original baseline parsed from `type`.

    # Original (FTMO) baseline — parsed from type ('challenge_100k' → 100k).
    original_baseline = 100_000.0
    t = (account.type or "").lower()
    for tok in t.split("_"):
        if tok.endswith("k") and tok[:-1].isdigit():
            original_baseline = float(tok[:-1]) * 1000.0
            break
    original_total_cap_dollars = (original_baseline
                                    * total_cap_pct / 100.0)
    re_anchored = (account.risk_baseline_equity > 0
                    and abs(baseline - original_baseline) > 1.0)

    today_pnl = today_real + unreal
    day_loss = max(0.0, -today_pnl)
    daily_used_pct = (day_loss / daily_cap_dollars * 100.0
                      if daily_cap_dollars > 0 else 0.0)
    daily_remaining = max(0.0, daily_cap_dollars - day_loss)

    # Total loss is always vs ORIGINAL FTMO baseline — that's what the
    # broker actually enforces. If the user re-anchored to a sandbox
    # baseline, we still show the truth here, not the anchor's picture.
    all_time_loss = max(0.0, original_baseline - eq) if eq else 0.0
    total_used_pct = (all_time_loss / original_total_cap_dollars * 100.0
                      if original_total_cap_dollars > 0 else 0.0)
    total_remaining = max(0.0, original_total_cap_dollars - all_time_loss)

    # Profit progress is shown against the user's anchor (so a sandbox
    # operator sees expected return %), with the FTMO target as a
    # secondary reference when re-anchored.
    profit = max(0.0, eq - baseline) if eq else 0.0
    profit_pct = (profit / profit_target_dollars * 100.0
                  if profit_target_dollars > 0 else 0.0)

    # Render two rows of 4 KPIs each
    row1 = target.columns(4)
    row1[0].markdown(_kpi("Balance", _money(bal),
                          color="#e5e7eb",
                          sub=f"baseline {_money(baseline)}"),
                     unsafe_allow_html=True)
    row1[1].markdown(_kpi("Equity", _money(eq),
                          color="#e5e7eb",
                          sub=f"vs balance {_money(eq - bal, sign=True)}"
                          if bal else None),
                     unsafe_allow_html=True)
    row1[2].markdown(_kpi("Unrealized", _money(unreal, sign=True),
                          color="#16a34a" if unreal >= 0 else "#dc2626"),
                     unsafe_allow_html=True)
    row1[3].markdown(_kpi("Today realized", _money(today_real, sign=True),
                          color="#16a34a" if today_real >= 0 else "#dc2626",
                          sub=f"{summary.n_trades_today if summary else 0} trades"),
                     unsafe_allow_html=True)

    row2 = target.columns(4)
    row2[0].markdown(_kpi("Daily-loss buffer", _money(daily_remaining),
                          color=_color_for_buffer(daily_used_pct),
                          sub=f"{daily_used_pct:.0f}% used · "
                              f"cap {_money(daily_cap_dollars)} "
                              f"({daily_cap_pct:.1f}%)"),
                     unsafe_allow_html=True)

    # Total-loss tile: always vs original FTMO baseline. Sub-text shows
    # the loss in $ and the original baseline so the operator sees the
    # actual floor they're being measured against.
    sub = (f"<b>${all_time_loss:,.0f}</b> lost of "
           f"{_money(original_total_cap_dollars)} cap "
           f"({total_cap_pct:.1f}% of {_money(original_baseline)})")
    if re_anchored:
        sub += "<br>FTMO floor — your re-anchored baseline doesn't change this"
    row2[1].markdown(_kpi("Total-loss buffer", _money(total_remaining),
                          color=_color_for_buffer(total_used_pct),
                          sub=sub),
                     unsafe_allow_html=True)

    # Profit-progress tile: always shows the FTMO-truth picture, NOT
    # the re-anchored personal baseline.
    #
    #   target_equity = original_baseline × (1 + profit_target_pct/100)
    #
    # Three states the operator might be in:
    #   1. eq >= target_equity         → ✅ Passed (overshoot %)
    #   2. baseline ≤ eq < target      → x% of way to target
    #   3. eq < baseline (the user's case)
    #          → "+y% to win" — the climb required FROM CURRENT EQUITY
    #            to hit the FTMO target. This is the number that matches
    #            'I need 20% more to win' — it's % gain on equity, not %
    #            of the original baseline.
    target_equity = original_baseline * (1.0 + profit_target_pct / 100.0)
    if eq <= 0:
        prof_value = "—"
        prof_color = "#9ca3af"
        prof_sub = "no equity reading"
    elif eq >= target_equity:
        overshoot = (eq / original_baseline - 1.0) * 100.0
        prof_value = "✅ Passed"
        prof_color = "#16a34a"
        prof_sub = (f"+{overshoot:.1f}% from {_money(original_baseline)} "
                    f"start · target was {profit_target_pct:.0f}%")
    elif eq >= original_baseline:
        # Above start, climbing toward target
        progress_pct = (eq - original_baseline) / (
            original_baseline * profit_target_pct / 100.0) * 100.0
        gap_dollars = target_equity - eq
        gap_pct = gap_dollars / eq * 100.0
        prof_value = f"{progress_pct:.1f}% of way"
        prof_color = "#16a34a"
        prof_sub = (f"+{(eq/original_baseline-1)*100:.1f}% from start · "
                    f"need <b>+{gap_pct:.1f}%</b> more to "
                    f"{_money(target_equity)} target")
    else:
        # Below start — show the climb required to win
        gap_dollars = target_equity - eq
        gap_pct = gap_dollars / eq * 100.0
        loss_pct = (original_baseline - eq) / original_baseline * 100.0
        prof_value = f"+{gap_pct:.1f}% to win"
        prof_color = "#dc2626"
        prof_sub = (f"down {loss_pct:.1f}% from "
                    f"{_money(original_baseline)} start · "
                    f"need {_money(gap_dollars)} to hit "
                    f"{_money(target_equity)}")
    row2[2].markdown(_kpi("Profit progress", prof_value,
                          color=prof_color, sub=prof_sub),
                     unsafe_allow_html=True)
    row2[3].markdown(_kpi("Next FTMO close",
                          _humanize(next_close_eta_s),
                          color="#e5e7eb",
                          sub=f"{next_close_local.strftime('%H:%M %Z')} · "
                              f"days traded {days_traded}/{days_required}"),
                     unsafe_allow_html=True)

    # Third row: market status across instrument classes the user cares about.
    # We show fx + index_us + index_eu + metal — users running other classes
    # see the relevant pill via classify(deployment.ticker).
    from core import market_clock
    classes_to_show = [
        ("FX",          "fx"),
        ("US indices",  "index_us"),
        ("EU indices",  "index_eu"),
        ("Metals",      "metal"),
    ]
    row3 = target.columns(len(classes_to_show))
    for col, (label, cls) in zip(row3, classes_to_show):
        s = market_clock.status_for_class(cls, now_utc=now)
        secs = s.seconds_until_change
        if s.is_open:
            color = "#16a34a"
            value = f"OPEN · closes in {_humanize(secs)}"
            sub = s.next_change_utc.strftime("%a %H:%M UTC")
        else:
            color = "#dc2626"
            value = f"CLOSED · opens in {_humanize(secs)}"
            sub = s.next_change_utc.strftime("%a %H:%M UTC")
        col.markdown(_kpi(f"{label} market", value, color=color, sub=sub),
                       unsafe_allow_html=True)
