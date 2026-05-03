# RUNBOOK — operating mt5_quant_trader_v2

This is the OPERATIONAL handbook. Anything here that isn't covered in tests
must still be true at runtime — when in doubt, halt and ask.

## 1. Daily startup checklist

Before placing any live or paper trade:

```bash
# 1. Run the gate
make test 2>&1 | tail -3       # 315+ passed expected

# 2. Refresh data (must be < 2 days old for live)
make refresh-data

# 3. Open the dashboard
make dashboard                  # → http://localhost:8502
```

In the dashboard:

- **Page 6 → Data Manager**: confirm all 30 cells are 🟢 (fresh < 2 days).
- **Page 5 → Account & Risk**:
  - FTMO daily progress bar < 50% (anything more = pause)
  - Time-guard countdowns visible (next weekend / daily flat)
  - EMERGENCY_STOP **not** active

If any of these are red, fix BEFORE doing anything else.

## 2. Adding a new strategy (the 60-second loop)

```bash
# Generate template via the wizard (Page 2) OR hand-edit:
$EDITOR strategies/my_idea.py

# Vet it
make screen STRATEGY=my_idea
# → docs/my_idea_screening.md
# Verdict ADD: best cell PF >= 1.3, R >= +0.15, n_test >= 25
# Verdict NEEDS_TUNING: refine params; re-run
# Verdict DISCARD: archive in docs/screened_out/

# If ADD: confirm replay parity is green
.venv/bin/pytest tests/test_replay_parity.py::test_parity_us100_d1[my_idea]
```

After parity is green, the strategy is eligible for paper portfolio (Page 3
Tab B). Live (Tab C) requires the additional pre-flight checks.

## 3. Going live — the 7 pre-flight gates

**Live orders cannot be sent unless ALL of these are green.**

| # | Gate | How to make it green |
|---|---|---|
| 1 | EMERGENCY_STOP file absent | `rm data/EMERGENCY_STOP` |
| 2 | account login in `live_safety.allowed_accounts` | edit `data/risk_config.json` |
| 3 | daily_loss_pct < `daily_loss_cap_pct` | wait for FTMO 22:00 UTC reset, or close out |
| 4 | consecutive_losses < `max_consecutive_losses` | wait for cooldown to expire |
| 5 | replay-parity for the strategy < 24h old | re-run replay (Page 3 Tab A) |
| 6 | idempotency key not seen in last 1h | (handled automatically) |
| 7 | NOT in no-entry window (last 30min before US close) | wait, OR plan trades earlier |

In addition: Page 3 Tab C requires the **"I understand this account already
failed once and may fail again"** checkbox to be ticked. This flips ONLY the
parity-recency gate; it does NOT bypass any other check.

## 4. Emergency stop

From ANY process:
```bash
touch data/EMERGENCY_STOP
```

Or from the dashboard: **Page 5 → Activate EMERGENCY_STOP** (with confirm modal).

The next call to `live_executor.send_order()` in any thread/process will refuse
the order, log the refusal, and raise `SafetyCheckRejection`. Closes are also
blocked — operators may need to remove positions manually via MT5 in extreme cases.

To lift: `rm data/EMERGENCY_STOP` (or Page 5 → Lift button).

## 5. Time-based forced exits (INVARIANT-8)

These are **non-overridable** in live mode by default:

- **Friday 19:55–20:00 UTC**: ALL open positions force-closed (`weekend_flat`).
- **Weekday 19:55–20:00 UTC**: stocks/indices force-closed (`daily_close_flat`).
  FX and metals are exempt.
- **Last 30 min before US close**: new entries refused (`no_entry_window`).

Reason these are hard-coded: FTMO accounts have been wiped by Sunday-night gaps
and overnight stock surprises. The backtest enforces the same rules so paper /
live behaviour matches it.

To disable: edit `data/risk_config.json` AND understand that this voids your
FTMO compliance. The dashboard does not provide a one-click toggle.

## 6. FTMO daily reset (22:00 UTC)

The `LiveRiskTracker.reset_daily(at_utc, day_start_balance)` call zeros
`consecutive_losses` and `daily_loss_pct` for ALL (symbol, strategy) cells and
records the reset in `ftmo_daily_resets`. The paper_loop should call this on
the first tick after 22:00 UTC; the dashboard's Page 5 should also offer a
manual "Trigger reset" button if needed (TODO: wire on Page 5).

## 7. Bridge degradation

If MT5 bridge stops responding:
- All `core.data.fetch_from_bridge` calls raise `TimeoutError` after 30s.
- The paper_loop catches and logs `error` events — it doesn't crash.
- The dashboard sidebar's bridge dot turns 🔴.
- New live orders will continue to BE GATED but the bridge call itself fails;
  the order is recorded as an `order_send` event with `ok=0`.

Do not silently re-issue. Diagnose the bridge first, then retry.

## 8. Data integrity

If a parquet's hash doesn't match what's recorded (INVARIANT-5), the loader
raises. To re-validate after a manual edit:

```bash
.venv/bin/python -c "from core.data import load_parquet; \
    df = load_parquet('data/US100.cash_D1.parquet'); print(len(df))"
```

If this raises, the parquet is corrupt — re-fetch via Page 6.

## 9. MT5 Strategy Tester parity (Phase 26)

Before promoting a strategy to live:

```bash
# 1. Generate the MQL5 scaffold
python scripts/strategy_to_mql5.py --strategy vol_breakout
# 2. Open mql5/VolBreakout.mq5 in MetaEditor
# 3. Translate the OnTick body (Python source is comment-embedded)
# 4. Compile (F7); drop on a chart matching ticker/TF
# 5. Run Strategy Tester with "Every tick based on real ticks"
# 6. Save report → data/mt5_tester_reports/<strategy>_<ticker>_<tf>.csv

python scripts/mt5_parity_check.py \
    --strategy vol_breakout --ticker US100.cash --tf D1
# → docs/parity_vol_breakout_US100.cash_D1.md
# Pass: count Δ ≤5%, P&L Δ ≤10%, per-trade match ≥80%
```

The parity gate (live_executor pre-flight #5) checks `parity_log` for a
recent (≤24h) passing entry. If absent, live orders for that strategy are
blocked — even if the override checkbox is on, that flag affects ONLY the
gate's RECENCY (a parity must have passed at least once).

## 10. Recovery from a crash

If the dashboard or paper_loop crashes mid-trade:

1. Risk-engine state is in `data/v2.db` and SURVIVES the crash.
2. Open positions in the broker remain — close manually via MT5 if needed.
3. Re-start the dashboard. The sidebar re-shows current state; check
   Page 5 for daily_loss_pct and any open positions.
4. Do NOT silently re-issue paper trades — `runner.tick`'s idempotency_key
   prevents double-opens, but a crashed-mid-flow position may have an
   inconsistent in-memory vs DB view.

## 11. FTMO pass-rate simulator

`make ftmo-sim` runs the Monte-Carlo bootstrap on the configured portfolio
and prints:
- P(pass), P(daily breach), P(total breach), P(no resolution)
- Per-strategy contribution to expected return AND DD

Use this BEFORE adding/removing strategies — the marginal contribution
column tells you whether a candidate is a tailwind or a drag.

```bash
make ftmo-sim ARGS="--iterations 20000"
make ftmo-sim ARGS="--target 5 --total-cap 10 --daily-cap 5"   # Phase 2
```

## 12. Audit trail queries

Every meaningful event is in `data/v2.db`:

```sql
-- Last 50 trades
SELECT closed_at_utc, mode, strategy, symbol, realized_pnl, close_reason
FROM trades WHERE realized_pnl IS NOT NULL
ORDER BY closed_at_utc DESC LIMIT 50;

-- Bridge events (latency + errors)
SELECT pinged_at_utc, method, ok, latency_ms, error
FROM bridge_events ORDER BY ROWID DESC LIMIT 100;

-- Forced flats (weekend + daily-close)
SELECT * FROM forced_flat_events ORDER BY occurred_at_utc DESC;

-- Pre-flight refusals (live_executor denials)
SELECT pinged_at_utc, method, error FROM bridge_events
WHERE method LIKE 'preflight_deny:%' ORDER BY ROWID DESC;
```

## 13. The non-negotiables

- Reconciliation gate IS the v1-bug-prevention mechanism. Do not weaken it.
- Replay-parity test is THE GATE before paper/live. Do not skip.
- Time-guard rules protect FTMO compliance. Do not disable casually.
- Idempotency keys prevent duplicate fills on retry. Do not reuse them.
- Emergency stop must work even when the dashboard is broken.

If any of these break, halt the system and root-cause before resuming.
