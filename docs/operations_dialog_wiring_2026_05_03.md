# Operations buttons now actually do something

Iterative round of UX fixes after dogfooding the page.

## What was wrong

| # | Issue | Symptom |
|---|---|---|
| 1 | Backtest / Paper / Go Live / Remove buttons rendered toasts but did nothing visible. | Operator clicked, status flipped silently, no idea what happened. |
| 2 | Go-Live "modal" rendered inline below the deployment cards. | On a long deployment list, modal was off-screen — looked broken. |
| 3 | Auto-seeded deployments used `vol_breakout` which has no entry in `grid_results.md`. | Every card showed "no backtest edge cached — run sweep_grid", undermining the whole Library wiring. |
| 4 | No view of "what's actually running". | Operator couldn't tell at a glance whether a paper run was alive, when it last ticked, what its day P&L was. |
| 5 | No persistent activity log. | Click Pause → toast → page reload → no record the click ever happened. |

## What changed

### `core/deployment.py` — `seed_survivor_deployments`

Replaced the hardcoded `vol_breakout × {US100, GER40, USDJPY, EU50, …}` list
with a derivation from `core.strategy_library`. The recommended portfolio
(⭐ entries) is seeded directly. When the catalog is missing, falls back
to a static four-row list that uses real surviving variants:
`ema_cross_9_20 USDJPY D1`, `ema_cross_12_26 GBPJPY D1`,
`ema_cross_9_20 GBPUSD D1`, `donchian_20 US100.cash D1`.

Cards are now backed by real grid stats out of the box.

### `dashboards/components/deployment_dialogs.py` (new)

Real `st.dialog` overlays for every action:

- **📊 Backtest** — runs the deployment's backtest inline on the cached
  parquet, shows reconciliation status, n_trades, win rate, profit
  factor, avg R, net P&L, and a Train/Test (60/40) split panel. Closes
  on demand. No more "open Page 1" hand-off.
- **📡 Paper** — confirms the parameters (account, risk, daily cap,
  time guards), starts the paper run, toasts the result.
- **🚀 Go Live** — pre-flight gates (EMERGENCY_STOP, daily-loss cap,
  parity recency, no-entry window, **market open** for the ticker,
  account allowlist) + risk math + DEPLOY-typed confirm. If any check
  is red, the deploy button stays disabled and the failing rows are
  visible.
- **🗑 Remove** — confirmation with full deployment context, plus a
  warning that removing a deployment doesn't close open positions.

Pause stays as a one-click toggle (no dialog) but now writes a
`pause_deployment` / `resume_deployment` event to the journal so the
activity log records it.

### `dashboards/components/active_runs_panel.py` (new)

A row-per-running-deployment table on a new **▶ Active runs** tab:

```
Deployment           Mode     Risk%   Started           Trades today  Realized today  Last close       Action
ema_cross_9_20       🟡 PAPER  0.30%   2026-05-03 14:02  3             +$48.20         2026-05-03 18:14 [⏹ Stop]
USDJPY · D1 · long
donchian_20          🟢 LIVE   0.30%   2026-05-03 09:31  1             −$36.00         2026-05-03 16:45 [⏹ Stop]
US100.cash · D1 · long
```

P&L coloured green/red. Stop button flips status back to idle and
audits.

### `dashboards/components/activity_log_panel.py` (new)

A reverse-chronological log of every operational event in the last
N hours (default 24, dropdown 1h–7d):

```
2026-05-03 18:14:31  📈  trade_close (paper)              ✓  ema_cross_9_20 USDJPY D1 long → $+24.50 (R +1.10) [target]
2026-05-03 18:00:02  🚀  ui_go_live_confirmed             ✓  deployment=donchian_20_US100_cash_D1_long risk_pct=0.3
2026-05-03 17:55:00  ⏸  pause_deployment                 ✓  deployment=ema_pullback_20_50_GBPJPY_D1_long
2026-05-03 14:02:11  📡  deploy_paper                      ✓  ema_cross_9_20 USDJPY D1 long
```

Pulls from `bridge_events` + `trades` tables. Every action that runs
through the dialogs writes here, so "did my click do anything?" is
answered visibly within one page reload.

### `dashboards/pages/0_🚀_Operations.py`

Tabs are now: **📊 Deployments | ▶ Active runs | 💼 Positions | 📜 Statement | 📈 Equity history | 📜 Activity log | 🛠 Settings**.

The card click handlers no longer toast — they call `open_dialog(kind, dep_id)`
which is rendered after the deployment list by
`render_active_dialog(...)`. Streamlit `st.dialog` provides a real
overlay; users can't miss it.

## Tests

- `tests/test_seed_survivor_uses_grid.py` — 5 new tests:
  - Seeds use grid survivors when catalog is present
  - Seeds are idempotent
  - Seeds carry the recommendation `why` notes through to `Deployment.notes`
  - Static fallback is used when the catalog is missing
  - Regression: no seeded deployment ever uses `vol_breakout` again
- Updated `tests/test_deployment.py::test_seed_survivors_idempotent` to
  not pin the count (which depends on whether `grid_results.md` is
  available).

**Test count: 478 → 483 (+5).**

## How to use

1. `make restart-dashboard`
2. Refresh `localhost:8502/Operations`
3. Cards now show edge chips (PF, R, n_test) for every recommended row
4. Click 📊 Backtest on any card → inline backtest results in a popup
5. Click 🚀 Go Live → real overlay modal with pre-flight gates
6. Open the **▶ Active runs** tab to see what's running with stop buttons
7. Open the **📜 Activity log** tab to see every action you've taken in
   the last 24 hours
