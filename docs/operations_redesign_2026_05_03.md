# Operations page redesign — 2026-05-03

The Operations page (localhost:8502/Operations) was the primary daily-driver
view but had several rough edges. This note documents what changed and why.

## Bugs found in the previous version

| # | Bug | Root cause |
|---|---|---|
| 1 | Sidebar showed truncated balance (`$90,2…`) and equity (`$91,3…`). | `st.metric` rendered inside a 1/4-width column couldn't fit a real $-amount. |
| 2 | "login `1`" everywhere. | Add-account form defaulted login to `0` and accepted any positive int — user typed `1` and hit enter. |
| 3 | "Account: 1" repeated on every deployment card. | Same login-=-1 plumbing surfacing in three places. |
| 4 | "Total loss vs 10% cap — 8,611 of 10,000 (86%)" before any deployment was even running. | `account_baseline=100_000` was hardcoded in `0_🚀_Operations.py`. The user's account had already failed the FTMO challenge so equity was below $100k from day zero. |
| 5 | "Go Live" button was bright red — Streamlit `type="primary"` defaults to red in dark mode, suggesting "danger" rather than "deploy". |
| 6 | Cards were sparse (`Started: —`, `Last signal: —`) for IDLE deployments — no information density. |
| 7 | No visible signal of whether a strategy *had* edge before going live — operator had to remember which cells were the survivors. |
| 8 | Position manager / statement / equity-chart were stacked below the deployment cards, requiring scroll. |

## Changes

### `core/account_manager.Account`

Extended with five new fields, all back-compatible (existing JSON loads without
migration):

```python
risk_baseline_equity: float = 0.0     # 0.0 → infer from `type`
daily_loss_cap_pct:   float = 5.0
total_loss_cap_pct:   float = 10.0
profit_target_pct:    float = 10.0
days_required:        int   = 5
```

`Account.effective_baseline_equity` parses the `type` string
(`challenge_100k` → 100 000, `funded_50k` → 50 000) and uses the override
when set. Two new helpers:

- `update_account(login, **changes)` — partial update on the registry
- `reset_baseline_to_current(login, current_equity)` — one-call helper for
  re-anchoring an already-blown account being re-used as a sandbox

### `core/edge_catalog.py` (new)

Reads `docs/grid_results.md` (output of `make sweep-grid`) and exposes
`best_for(ticker, tf, strategy_prefix)` so the deployment card can show
the cell's backtest stats inline. Tolerant — missing/malformed file
returns `{}`, never raises.

### `dashboards/components/theme.py` (new)

Single `inject_css()` call that:

- forces tabular numerals everywhere (`font-variant-numeric: tabular-nums`)
- restyles the "primary" button to **green** (Go Live now reads as deploy,
  not danger)
- tightens metric padding
- gives custom KPI tiles a consistent dark card frame

### `dashboards/components/kpi_strip.py` (new)

Full-width 2 × 4 grid of KPI tiles at the top of Operations, replacing the
squashed sidebar:

```
Balance | Equity | Unrealized | Today realized
Daily-loss buffer | Total-loss buffer | Profit progress | Next FTMO close
```

Each tile uses monospace tabular numerals and is colour-coded:
- green when positive / safe
- amber 60-80 % of an FTMO cap
- red ≥ 80 % of cap or negative P&L

### `dashboards/components/deployment_card.py`

- Inline backtest-edge chip — `EDGE`, `OOS only`, `IS only`, or `negative`
  with PF / R / n_test stats next to it. Pulled from `edge_catalog`.
- Status badge restyled (rounded pill, monospace).
- "Account: 1" line removed (the account context is now in the page header).
- Em-dash noise gone — `Started: —` only renders when actually known.

### `dashboards/components/account_switcher.py`

- Login is now a text input with a digits-≥-5 validator (real MT5 logins are
  6-9 digits). No more `0`-default footgun.
- Form asks for `risk_baseline_equity` directly, with sensible per-`type`
  defaults pre-filled.

### `dashboards/components/live_equity_chart.py`

- Daily realized-PnL bars overlayed on the cumulative-balance line
- Mark lines for baseline, daily-loss floor, total-loss floor, profit target
- Dark-on-dark Plotly theme matching the rest of the page

### `dashboards/components/position_manager_panel.py`

- Pandas Styler with green/red P&L colouring and 5-decimal price formatting
- Header strip shows count, summed unrealized, and average R-multiple
- Per-row Close button stamped inline

### `dashboards/pages/0_🚀_Operations.py`

Layout reorganised top-to-bottom:

1. Emergency-stop banner
2. Account switcher
3. Account meta line + "⚠ Re-anchor baseline" popover (auto-shown when
   the account has crossed 90 % of its total-loss cap)
4. **KPI strip** (full width)
5. Tabs: **📊 Deployments | 💼 Positions | 📜 Statement | 📈 Equity history | 🛠 Settings**
6. Add-deployment expander, Go-Live modal stay where they were

The Settings tab finally lets the operator edit per-account caps, baseline,
profit target, and trading-day requirement without manually touching JSON.

## Multi-TF strategy sweep — 2026-05-03

Ran `make sweep-grid` on the freshly-refreshed parquets (8 tickers ×
3 timeframes × 7 strategies = 168 cells). Reconciliation invariant held
on every cell.

**Edge candidates (positive in BOTH train and test):** 13

Top out-of-sample R-multiples on real broker data:

| ticker | tf | strategy | n_test | test_PF | test_R |
|---|---|---|---:|---:|---:|
| USDJPY | D1 | ema_cross_9_20 | 13 | 3.74 | +0.95 |
| GBPJPY | D1 | ema_cross_12_26 | 8 | 3.64 | +0.94 |
| USDJPY | D1 | ema_cross_12_26 | 11 | 3.65 | +0.86 |
| GBPUSD | D1 | rsi_30_70 | 6 | 4.95 | +0.81 |
| GBPUSD | D1 | ema_cross_9_20 | 13 | 2.18 | +0.61 |
| US100.cash | D1 | ema_cross_12_26 | 7 | 2.39 | +0.59 |
| US100.cash | D1 | donchian_20 | 19 | 2.23 | +0.55 |
| GBPJPY | D1 | ema_cross_9_20 | 12 | 1.96 | +0.51 |
| US100.cash | D1 | donchian_55 | 14 | 1.82 | +0.37 |

Full table: `docs/grid_results.md`.

## Test count

- baseline: **397**
- after redesign: **415** (+ 18 new)
  - `test_account_baseline_inference.py` — 10 tests (Account model + persistence)
  - `test_edge_catalog.py` — 8 tests (markdown parser, best-for query, tolerance to malformed input)

## Operational notes

- The bogus `accounts/1/` directory was archived to
  `accounts/_archived_login_1_<timestamp>/` and `data/accounts.json` reset
  to `[]` so the operator re-adds the account through the new validated form.
- Existing `data/v2.db` and per-strategy edge caches untouched.
