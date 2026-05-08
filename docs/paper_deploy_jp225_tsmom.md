# Paper-deploy guide — JP225 D1 TSMOM (the new cell)

## What just shipped

- **`strategies/tsmom.py`** — AQR-faithful Time-Series Momentum (Moskowitz/Ooi/Pedersen 2012) — auto-discovered by the runner registry, no further wiring needed
- **`docs/optimization_tsmom_2026-05-05.md`** — catalog row so Strategy Library + Composer + Backtest see the cell
- **`core/edge_catalog.py`** — tag-aware recovery gate (90d for meanrev/breakout, 180d for trend) so future trend strategies don't get rejected for their inherent drawdown profile

The new cell loads cleanly — `tsmom JP225.cash D1 PF_test=1.90 score=18.7 deploy_safe=True`. **All 860 tests passing.**

## Deploy as paper — three steps

### 1. Verify the cell appears in the dashboard

Restart your Streamlit dashboard so the new strategy file + catalog markdown get picked up:

```bash
make dashboard
```

Then in the browser:

- **Strategy Library** (page 7) → search `tsmom` — should show one row for JP225.cash D1
- **Strategy Compare** (page 8) → tsmom should appear in the strategy filter
- **Backtest** (page 1) → strategy dropdown includes `tsmom`

### 2. Run a paper-mode backtest first (sanity check)

On the Backtest page:
- Strategy: `tsmom`
- Ticker: `JP225.cash`
- TF: `D1`
- Cost: defaults ($4 + 0.05×ATR)
- Click Run

Expected output: PF_test ≈ 1.90, n_test ≈ 26, R_test ≈ +0.13. If you see materially different numbers (>10 point divergence in score), the data window has drifted since today — this is normal as new D1 bars accumulate.

### 3. Deploy as paper

Two options:

**Option A — through the Composer (recommended):**
- Portfolio Composer (page A) → Recommended preset
- The new tsmom JP225 D1 cell will show in the candidate pool with checkboxes
- Tick it, leave others as-is
- Risk %: 0.05 (smaller than mean-reversion cells because TSMOM has wider DDs by construction)
- Daily cap: 1.0% (the position holds for ~21 D1 bars, so daily exposure is just one position open)
- Click "Deploy as paper"

**Option B — through Strategy Library deep-dive:**
- Strategy Library → click on the tsmom row
- Deep-dive panel opens
- Deploy → set status=paper, risk_pct=0.05, daily_cap_pct=1.0, long_only=False
- Save

## What to expect over the paper window

- **Trade frequency:** ~2 trades/month on D1 (the strategy waits for monthly rebalance). Don't be alarmed by long quiet periods.
- **First signal:** could be any day. The strategy emits a signal on the next D1 bar after deployment whenever |12-month return| > 0.5%.
- **Live R per trade:** target +0.13R (catalog expectation). Track this on the Performance page (page 4) under the Paper tab.
- **Convergence:** with only 2 trades/month, you need 4-6 months for live-vs-catalog comparison to be statistically meaningful. The autolearner (#269 next) will give earlier z-score warnings.

## When to consider promoting to live

Promote criteria — ALL must hold:
- ≥ 8 closed paper trades (~4 months of paper time)
- Live R between catalog R and catalog R - 1σ (i.e. at least −0.10R/trade live, not significantly below catalog +0.13)
- No adverse regime change visible in the equity curve (no >15% paper DD)
- Replay-parity check passes when run after 8 trades

If any of those fail, keep on paper or demote to idle. Don't promote based on a few good trades — the small sample lies both ways.

## What I recommend now

The JP225 paper deploy is a hands-off task — fire it, then leave it for 4-8 weeks. While it accumulates data:

**Next on the build queue** (per the priority list):

1. **#269 edge-decay autolearner** — most valuable single piece. Makes the JP225 cell (and all cells) self-monitoring. Without it, you're checking R manually on Performance page.
2. **#279 pairs trading** — adds market-neutral diversification. Will likely produce 2-3 deploy-safe pairs.
3. **#262 news calendar feed** — blocks opens around NFP/FOMC, additive safety
4. **#275 LLM macro overlay** — weekly Claude-powered regime classifier

The autolearner (#269) is what I'd pick up next — it's purely defensive code (no live capital at risk during build), and it makes every cell after it more trustworthy.

---

*Files:*
- `strategies/tsmom.py`
- `scripts/sweep_tsmom.py`
- `docs/optimization_tsmom_2026-05-05.md` (catalog row)
- `docs/tsmom_results_2026_05_05.md` (full results)
- `docs/institutional_strategies_research.md` (where this came from)
