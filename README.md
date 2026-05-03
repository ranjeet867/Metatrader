# Metatrader — MT5 Quant Trader

[![tests](https://github.com/ranjeet867/Metatrader/actions/workflows/test.yml/badge.svg)](https://github.com/ranjeet867/Metatrader/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![tests passing](https://img.shields.io/badge/tests-539%20passing-brightgreen.svg)](#testing)

> **Production-grade MetaTrader 5 quant trading research and execution
> platform — built test-first, with 13 hard invariants enforced everywhere.**

A full FTMO-aware operations centre for systematic traders: backtest
sweeps with reconciliation gates, walk-forward validation, Monte-Carlo
FTMO pass-rate simulation, dynamic position sizing, time-guard rules,
and a live operations dashboard with multi-account support.

---

## Screenshots

> Add four screenshots to `docs/screenshots/` after first run:
> `01_operations.png`, `02_strategy_library.png`, `03_backtest_dialog.png`,
> `04_data_manager.png`. The README references them below.

| Operations control centre | Strategy Library |
|---|---|
| ![Operations](docs/screenshots/01_operations.png) | ![Strategy Library](docs/screenshots/02_strategy_library.png) |
| KPI strip + tabs for Deployments / Active runs / Positions / Statement / Equity / Activity log / Settings | Curated portfolio with PF, R, win-rate, edge chips and a train/test scatter |

| Backtest dialog | Data Manager |
|---|---|
| ![Backtest](docs/screenshots/03_backtest_dialog.png) | ![Data Manager](docs/screenshots/04_data_manager.png) |
| Inline backtest with equity + drawdown curves, FTMO floors, full stats grid | Per-ticker freshness with one-click Fetch ALL |

---

## Why this exists

Most retail FTMO/prop-firm tooling is a black box: you buy a strategy,
you click Go Live, you have no idea what the numbers mean or whether the
simulation lies. v2 fixes that with a single rule:

> **Every dollar of P&L the dashboard shows must reconcile with the
> equity curve the backtest produced. If they don't match, the result
> is rejected.**

Out of that one invariant fall the other twelve.

## Hard invariants

| # | Invariant | Test |
|---|---|---|
| 1 | **Reconciliation** — `sum(closed_trade.pnl) == equity_curve[-1] - equity_curve[0]` within $0.01 | every result page shows the exact $ divergence value |
| 2 | **Replay-parity** — replay produces same trades + PnL as backtest for ALL strategies | `tests/test_replay_parity.py` (23 tests) |
| 3 | **Single PnL calculator** — backtest, replay, paper, live all import `compute_realized_pnl` from `core.backtest` | `tests/test_paper_executor::TestPnLParityWithBacktest` |
| 4 | **Idempotency** — same input + same code → byte-identical output | `tests/test_idempotency_real_data.py` |
| 5 | **No silent fallbacks** — bridge timeouts raise; bad config raises; never silent defaults | `core/config.py` validation |
| 6 | **No live orders without 8 gates** — emergency stop, allowlist, daily cap, consecutive losses, parity recency, idempotency dedup, no-entry window, **market-open** | `tests/test_live_executor_safety.py` (14 tests) |
| 7 | **Reproducible builds** — all randomness seeded (default 42); no clock-based seeds | `tests/test_ftmo_simulator.py` |
| 8 | **Time-based forced exits** — weekend-flat, daily-close-flat, FTMO pre-close in user tz | `tests/test_time_guards.py` + `tests/test_ftmo_clock.py` |
| 9 | **Account isolation** — A's trades never appear in B's journal | `tests/test_multi_account_isolation.py` |
| 10 | **Arithmetic risk floors** — `effective_daily_cap_pct ≤ ftmo_daily_loss_pct × 0.8` | risk engine pre-trade |
| 11 | **Go-Live confirmation** — typed `DEPLOY` + all gates green | `tests/test_go_live_modal_blocks_when_check_fails.py` |
| 12 | **Phantom-position safety** — system NEVER auto-closes a position it didn't open | `tests/test_position_manager.py` |
| 13 | **Reconcile idempotency** — running broker reconcile twice yields identical journal | `tests/test_position_manager.py` |

## Features

### Operations dashboard (`localhost:8502`)

- **Auto-detect MT5 account** — one click on **🔍 Auto-detect from MT5**
  pulls login, broker, server, balance, leverage, currency, and detects
  your local timezone from the system. No more typing 9-digit logins.
- **KPI strip** — balance, equity, unrealized, today P&L, FTMO daily
  buffer, FTMO total-loss buffer (always vs original challenge baseline,
  not your re-anchored personal anchor), profit progress (shows "+X% to
  win" when below baseline), next FTMO close countdown in your tz, plus
  a market-status row for FX / US indices / EU indices / Metals.
- **Tabs** — Deployments, Active runs, Positions, Statement, Equity
  history, Activity log, Settings.
- **Real-modal dialogs** — `📊 Backtest`, `📡 Paper`, `🚀 Go Live`,
  `🗑 Remove` open `st.dialog` overlays with full context. Backtest runs
  inline and shows equity + drawdown curves, FTMO −5% / −10% floors,
  win rate, profit factor, R:R ratio, expectancy, CAGR, max consec
  losses, recovery days, and a train/test split panel.

### Strategy Library

A curated, opinionated list of deployable cells (strategy × ticker × tf
× R:R) ranked by an FTMO-survival-weighted score. Read-only; the
deployment side lives elsewhere. Filters by recommended-only, edge-only,
ticker substring, timeframe. Includes a train_R × test_R scatter
chart with a shaded survivor zone.

### Autonomous portfolio optimizer

```bash
python scripts/optimize_portfolio.py --top 30 --risk 0.5
```

Sweeps `(strategy × ticker × tf × R:R variant)`, runs each cell through
the reconciliation-gated backtester, computes full stats (drawdown %,
recovery days, max consecutive losses, R:R ratio, Sharpe-on-R), runs a
30-day FTMO Monte-Carlo at the user's chosen risk %, and writes a ranked
report to `docs/optimization_<date>.md`. The dashboard's Strategy
Library reads that file automatically.

### FTMO compliance

- **Time guards** — weekend-flat (Friday 19:55 UTC), daily-close-flat
  for indices (weekday 19:55 UTC), FTMO pre-close 5 minutes before
  22:00 UTC reset, all configurable.
- **Pre-flight gates** — 8 checks must pass before a single live order
  goes out: EMERGENCY_STOP absent, daily-loss within cap, replay-parity
  recent (< 24h), no-entry window, market open for ticker, account in
  allowlist, idempotency dedup, plus typed `DEPLOY` confirmation.

### Position management

- Per-position close + typed-confirm Close-All
- Detects manual closes done in MT5 terminal (writes them to journal)
- Phantom-position warning — broker has it, we don't track it →
  banner only, never auto-close

### Multi-timeframe data

Cached parquets per (ticker, tf) refreshed via the bridge. Built-in
gap analysis. Bridge-latency dashboard.

---

## Setup

### Prerequisites

- **Python 3.11 or 3.12** (3.10 works for tests; dashboard prefers 3.11)
- **MetaTrader 5** with a working broker (FTMO / TopStep / etc.)
- **MT5 file-bridge EA** running on the chart you want to query.
  - The bridge is a small MQL5 script that reads JSON requests from a
    file and writes responses. The repo expects RPC methods
    `account_info`, `symbol_info`, `copy_rates`, `positions_get`,
    `position_close`, `history_deals_get`. See
    [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for the EA template.
- **macOS or Linux** for the dashboard host (Windows works with WSL).

### Installation

```bash
git clone https://github.com/ranjeet867/Metatrader.git
cd Metatrader
make setup        # creates .venv, installs deps
make test         # all tests must pass (539+)
```

### First run

```bash
# 1. Refresh price data from the running MT5 bridge
make refresh-data

# 2. Run the autonomous optimizer to populate the strategy library
python scripts/optimize_portfolio.py --top 30 --risk 0.5

# 3. Start the dashboard
make dashboard
```

Open `http://localhost:8502/Operations` and click **🔍 Auto-detect
from MT5** to register your account.

### Make targets

| target | what it does |
|---|---|
| `make setup` | Create venv + install deps |
| `make test` | Run all tests (must be green to ship) |
| `make test-fast` | Skip slow / integration tests |
| `make test-cov` | Run with coverage report |
| `make lint` | Ruff lint |
| `make refresh-data` | Pull every parquet from the MT5 bridge |
| `make backtest` | Single-strategy backtest from terminal |
| `make sweep-strategies` | Compare strategies on one (ticker, tf) |
| `make sweep-grid` | Multi-ticker × multi-TF × multi-strategy grid |
| `make screen STRATEGY=<name>` | Vet a new strategy, generates `docs/<name>_screening.md` |
| `make ftmo-sim` | FTMO pass-rate Monte-Carlo |
| `make dashboard` | Launch Streamlit on `:8502` |
| `make restart-dashboard` | Kill running dashboard + relaunch |

---

## Architecture

```
core/
  config.py             — read/write data/risk_config.json
  asset_class.py        — classify symbols (stock|index|metal|energy|fx)
  market_clock.py       — open/closed/next-event per asset class
  time_guards.py        — INVARIANT-8 schedule logic
  ftmo_clock.py         — FTMO daily-reset + pre-close timezone math
  data.py               — load + validate candles; MT5 bridge fetch
  indicators.py         — pure indicators; analytic-math tested
  storage.py            — SQLite schema + idempotent migrations
  strategy.py           — Strategy protocol + Signal dataclass
  backtest.py           — reconciliation-enforced backtester +
                            compute_realized_pnl (the single PnL formula)
  backtest_stats.py     — DD %, recovery days, R:R, expectancy, CAGR
  paper_executor.py     — single-position paper engine (INVARIANT-3)
  runner.py             — atomic per-bar tick (used by replay/paper/live)
  replay.py             — bar-by-bar replay using PaperExecutor (gate)
  paper_loop.py         — multi-strategy paper loop with heartbeat
  risk_engine.py        — PositionSizer + LiveRiskTracker (persisted)
  position_sizer.py     — calc_lots from risk% × stop distance × $/tick
  position_manager.py   — Close one / close all + reconcile_with_broker
  account_statement.py  — broker-truth daily/total PnL view
  account_manager.py    — multi-account registry, FTMO baselines
  account_detect.py     — bridge-driven auto-detect of login + tz + broker
  edge_catalog.py       — reads grid_results.md + optimization_*.md
  strategy_library.py   — curated portfolio with edge stats
  optimizer.py          — autonomous (strategy × ticker × tf × R:R) sweep
  journal.py            — append-only audit trail
  mt5_account.py        — bridge-backed account/symbol metadata client
  live_executor.py      — 8 pre-flight gates before any real order
  parity_gate.py        — replay-parity-recency check for live #5
  ftmo_simulator.py     — Monte-Carlo bootstrap pass-rate

strategies/             — 11 strategies, ALL replay-parity tested
  vol_breakout.py
  rsi_meanrev.py
  ema_cross.py
  ema_pullback.py
  bbands_meanrev.py
  donchian_breakout.py
  ibs.py
  inside_bar.py
  orb.py
  overnight_drift.py
  first30_meanrev.py

dashboards/
  app.py                — multi-page entry, sidebar, navigation
  pages/
    0_🚀_Operations.py        — daily-driver front door
    1_📊_Backtest.py
    2_🔬_Strategy_Studio.py
    3_📡_Paper_Live.py
    4_📈_Performance.py
    5_⚙️_Account_Risk.py
    6_💾_Data_Manager.py
    7_🏛️_Strategy_Library.py
  components/
    kpi_strip.py              — full-width KPI band
    deployment_card.py        — one card per (strategy × ticker × tf)
    deployment_dialogs.py     — st.dialog overlays
    active_runs_panel.py      — what's currently running
    activity_log_panel.py     — operational events + closed trades
    position_manager_panel.py — open positions table
    statement_panel.py        — today's broker-truth picture
    live_equity_chart.py      — equity history with FTMO floors
    ftmo_progress.py          — FTMO compliance bars
    emergency_stop_bar.py     — sticky banner with typed-confirm clear
    account_switcher.py       — auto-detect + manual entry
    theme.py                  — global CSS

scripts/
  refresh_all_data.py
  screen_strategy.py
  sweep_grid.py
  sweep_strategies.py
  optimize_portfolio.py       — autonomous (strategy × R:R) optimizer
  ftmo_sim.py
  strategy_to_mql5.py
  mt5_parity_check.py

docs/
  RUNBOOK.md                  — operational procedures
  index_edge_findings.md
  autonomous_optimization_findings_2026_05_03.md
  operations_redesign_2026_05_03.md
  dashboard_pages_explained_2026_05_03.md
  operations_dialog_wiring_2026_05_03.md
```

## Ports + connectors

| port | service |
|---|---|
| `8502` | Streamlit dashboard |
| MT5 file-bridge | local file watch — no TCP port |

The bridge writes to / reads from a JSON file in your MT5 `MQL5/Files/`
directory. The Python side polls that file. See
[`docs/RUNBOOK.md`](docs/RUNBOOK.md) for the exact format.

---

## Testing

```bash
make test           # 539+ tests, ~5s
make test-fast      # skip slow + integration
make test-cov       # with coverage report
make lint           # ruff
```

Tests use **synthetic fixtures** for everything that needs broker data,
so the full suite runs offline without an MT5 connection.

---

## Contributing

PRs welcome. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first — every
change must:

1. Keep all 13 invariants enforced
2. Pass `make test` + `make lint`
3. Add tests for new logic (we don't accept untested code)

---

## License

MIT — see [`LICENSE`](LICENSE).

**Disclaimer:** trading is risky. This is research software. Past
backtest performance does not predict live results. The authors take
no responsibility for losses.

---

## Related reading

- [Operations redesign notes](docs/operations_redesign_2026_05_03.md)
- [Dashboard pages explained](docs/dashboard_pages_explained_2026_05_03.md)
- [Autonomous optimization findings](docs/autonomous_optimization_findings_2026_05_03.md)
