# Dashboard pages — what each one is for

After this round of simplification.

## 🚀 Operations (Page 0) — daily-driver

What you see when you open `localhost:8502/Operations`. KPI strip across
the top (balance, equity, unrealized, today P&L, FTMO buffers, profit
progress, next forced flat), then deployment cards by tab. Switch
between accounts at the top — auto-detect button pulls login + server +
balance + leverage + your local timezone from the running MT5 bridge.

You don't pick what to deploy from here. You **monitor** what's already
deployed.

## 📊 Backtest (Page 1)

Single-strategy backtest with reconciliation enforcement. For research,
not for choosing what to run live.

## 🔬 Strategy Studio (Page 2)

A/B compare, parameter sweep, walk-forward. Also research.

## 📡 Paper & Live (Page 3) — **simplified**

Four tabs. The **Live** tab is the one that changed:

- Old: a wall of green ✅ / red ⛔ pre-flight checkboxes plus a hidden
  text input for strategy name. Operator had no way to know *what to
  deploy*.
- New:
  - Compact health pill at the top (`All checks ✅ (3/3)` or
    `⚠ 1 check failing`). Click to expand details.
  - **Strategy portfolio picker** — every entry from
    `core.strategy_library` as a checkbox row. Recommended (⭐) ones
    pre-checked. Each row shows PF/R/n_test inline so you can see what
    you're picking.
  - Risk % per row (default 0.30%, editable).
  - Big green "🚀 Deploy to live" button that pushes the picks into
    your active account's `deployments.json`.

## 📈 Performance (Page 4)

Combined trade journal across backtest / paper / live. Shows:

- Top KPI strip — trades, win rate, profit factor, avg R, net P&L
- Cumulative P&L curve and drawdown curve
- R-multiple histogram and per-strategy attribution donut
- Monthly P&L heat map (year × month)
- Editable trade journal (add notes inline)

Sidebar filters by mode / strategy / symbol / close-reason. The page
pulls from `data/v2.db` and from per-account `data/accounts/{login}/v2.db`
once you start running things.

## ⚙️ Account & Risk (Page 5) — **simulator now driven by library**

FTMO progress, per-strategy risk state, time guards, EMERGENCY_STOP,
position sizing preview, risk caps editor, and the FTMO **pass-rate
simulator**.

Old simulator: hardcoded `vol_breakout` on 3 tickers.
New simulator: a multi-select of every entry in the Strategy Library.
Recommended ones default-checked. Click Run → bootstraps OOS R from each
pick on real broker data → shows P(pass), P(daily breach), P(total
breach), expected final %, and per-strategy contribution to return + DD.

## 💾 Data Manager (Page 6) — **batch fetch**

- **🔄 Fetch ALL** button at top — one click refreshes every parquet.
- Per-ticker card with `⬇ Refresh all TFs` button.
- Per-TF row with single `⬇` button + freshness colour dot + last-bar
  timestamp + filesystem path.
- `➕ Add a new ticker` expander.
- `🔍 Gap analysis` and `📡 Bridge latency` collapsed by default.

## 🏛️ Strategy Library (Page 7 — NEW)

The "what should I deploy?" reference page. Read-only.

- Top KPI: total cells, recommended count, survivors, average test_R
- Filters: ⭐ recommended only, ✅ with edge only, ticker substring, tf
- **Scatter chart** — train_R on x, test_R on y, marker size = n_test.
  The shaded green quadrant in the upper-right is the survivor zone
  (positive on both axes). ⭐ Recommended points are gold; others blue.
- Sortable table — every cell with PF / R / n_test / why-recommended.
  Green/red colouring on the R and PF columns.
- A "How to use" footnote linking back to Operations / Live / sweep-grid.

## How the pieces fit

```
Data Manager        →  refresh real broker parquets
        ↓
make sweep-grid     →  evaluates 7 strategies × 8 tickers × 3 timeframes
                       writes docs/grid_results.md (the truth)
        ↓
Strategy Library    →  reads grid_results.md, ranks recommended-first
        ↓
Live tab (Page 3)   →  pre-checks the recommended portfolio
                       you tweak risk %, click Deploy
        ↓
Operations (Page 0) →  KPI strip + tabs to monitor and manage
                       deployment cards show edge chips inline
                       position manager + statement + equity history
        ↓
FTMO Simulator      →  pick from Library, bootstrap OOS R, show pass rate
(Page 5)
        ↓
Performance (Page 4)→  combined journal across backtest/paper/live
                       with R-distribution, drawdown, monthly heatmap
```

The "Daily-driver" workflow:

1. Open **Operations** — glance at KPI strip and FTMO buffers.
2. Need new edge? Open **Data Manager** → click *Fetch ALL* → run
   `make sweep-grid` from terminal → open **Strategy Library** → review.
3. Want to deploy? Open **Live** tab → check the survivors → set risk →
   *Deploy to live*.
4. Want to know expected pass rate? Open **Account & Risk** → simulator
   multi-select → Run.
5. End of day? Open **Performance** → review which strategies fired and
   where the P&L came from.
