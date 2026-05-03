# v2 Control Dashboard

A Streamlit dashboard that wraps the v2 reconciliation-enforced backtester.
Every result on every screen goes through `core.backtest.run_backtest` — the
dashboard is a VIEW + CONTROL layer that never re-implements PnL math.

## Run it

```bash
make dashboard            # opens at http://localhost:8501
# or directly:
.venv/bin/streamlit run dashboards/control.py
```

If the deps aren't installed yet:

```bash
.venv/bin/pip install streamlit plotly       # already pinned in pyproject.toml
```

## Tabs

### 🔬 Backtest
- Pick ticker / timeframe / strategy in the sidebar
- Strategy params form is **auto-generated** from the strategy's `Params`
  dataclass (so all 11 strategies are usable out of the box without dashboard
  code changes)
- Friction inputs: lots, commission, slippage (× ATR), train fraction
- Click **Run Backtest** to see:
  - Reconciliation badge (green ✓ or red ⛔ — if red, do NOT trust the numbers)
  - Headline metrics row (trades, win%, PF, avg R, return %)
  - Train / test partition stats
  - Equity curve (Plotly) with the train/test boundary marked
  - Drawdown chart underneath
  - Sortable trade tape
  - Close-reason histogram

Example: select **US100.cash D1 vol_breakout** with default params and lots=6.5
→ should see ✅ Reconciled, ~46 OOS trades, ~+41.7 % test return, the equity
chart climbing from the train/test split point.

### 🔄 Data Refresh
- Lists every cached parquet under `data/` with last-modified timestamp
- "Refresh from MT5 Bridge" button calls `core.data.fetch_from_bridge` directly
  (the same code path `scripts/load_real_data.py` uses)
- Times out gracefully if the EA on the Wine VM isn't running

### 🧮 Sweep
- Multi-select tickers / timeframes / strategies
- Runs every cell through `run_backtest`, partitions train/test, halts on any
  reconciliation failure
- Heatmap of mean test_R (out-of-sample R per trade), red→green
- Survivors table filtered by acceptance criteria you set in the form
  (defaults: n_test ≥ 20, test_PF ≥ 1.20, test_R ≥ 0.10, train+test both positive)

### 📝 Paper (stub)
- **Does NOT send orders.** Writes a JSON state file under
  `data/paper_state/<strategy>__<ticker>.json` and prints what an order
  *would* look like.
- A real executor module + verified MT5 Strategy Tester parity is required
  before this can be wired live.

## Reconciliation invariant

Every backtest run on this dashboard checks
`sum(closed_trade.realized_pnl) == equity_curve[-1] - equity_curve[0]`
within $0.01. If the assertion ever fails, you'll see a big red banner at
the top of the Backtest tab — and a list of failed cells in the Sweep tab.

That gate is the same one the test suite enforces; the dashboard cannot bypass
it because it doesn't compute PnL — only `run_backtest` does.

## Tests

```bash
make test                                  # all 115 tests green
.venv/bin/pytest tests/test_dashboard_smoke.py -v
```

The smoke tests verify:
- The module imports cleanly (no startup-time errors)
- All 11 strategies are discoverable
- `run_one()` produces the same reconciling result that `run_backtest` would
- The Plotly helpers return real `go.Figure` objects

## Architecture note

`dashboards/control.py` imports nothing strategy-specific. It introspects
`strategies/*.py` at runtime to find every class with a `name` attribute and
a paired `*Params` dataclass, then auto-builds the form. To add a new strategy
to the dashboard, just drop a new file in `strategies/` — no dashboard edits.
