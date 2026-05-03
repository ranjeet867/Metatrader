# mt5_quant_trader_v2

**Production-grade MT5 quant trading research — rebuilt from scratch with rigor.**

## Why v2

v1 had bugs across multiple layers (executor balance not updating, divergent metric paths, vec_engine vs strategy_engine drift, EMERGENCY_STOP silently halting backtests). Every backtest result above the broken substrate was unreliable.

v2 is built test-first, with **8 hard invariants** enforced everywhere — no exceptions, no skip lists, no tolerance widening.

## Hard invariants

| # | Invariant | Test |
|---|---|---|
| 1 | **Reconciliation**: `sum(closed_trade.pnl) == equity_curve[-1] - equity_curve[0]` within $0.01 | every result page shows the EXACT $ divergence value |
| 2 | **Replay-parity**: replay produces same trades + PnL as backtest for ALL 11 strategies | `tests/test_replay_parity.py` (23 tests) |
| 3 | **Single PnL calculator**: backtest, replay, paper, live all import `compute_realized_pnl` from `core.backtest` | guarded by tests/test_paper_executor::TestPnLParityWithBacktest |
| 4 | **Idempotency**: same input + same code → byte-identical output (md5 checksum on real US100 D1) | `tests/test_idempotency_real_data.py` |
| 5 | **No silent fallbacks**: bridge timeouts raise; bad config raises; never silent defaults | `core/config.py` validation; `core/data.py` raise on bridge timeout |
| 6 | **No live orders without 7 gates**: EMERGENCY_STOP, account allowlist, daily loss cap, consecutive losses, parity recency, idempotency dedup, no-entry window | `tests/test_live_executor_safety.py` (14 tests) |
| 7 | **Reproducible builds**: all randomness seeded (default 42); no clock-based seeds | `tests/test_ftmo_simulator.py::test_seeded_rng_is_deterministic` |
| 8 | **Time-based forced exits**: weekend-flat (Friday 19:55 UTC, all classes); daily-close-flat (weekday 19:55, stocks/indices) | `tests/test_time_guards.py` + `tests/test_backtest_with_time_guards.py` |

## Quickstart

```bash
make setup            # create venv, install deps
make test             # all tests must pass (315+ as of 2026-05)
make refresh-data     # pull all parquets from MT5 bridge (when bridge running)
make dashboard        # launches Streamlit multi-page UI on :8502
```

## Project layout

```
core/
  config.py          — read/write data/risk_config.json
  asset_class.py     — classify symbols (stock|index|metal|energy|fx|other)
  time_guards.py     — INVARIANT-8 schedule logic
  data.py            — load + validate candles; MT5 bridge fetch
  indicators.py      — pure indicators; analytic-math tested
  storage.py         — SQLite schema + idempotent migrations
  strategy.py        — Strategy protocol + Signal dataclass
  backtest.py        — reconciliation-enforced backtester +
                        compute_realized_pnl (THE single PnL formula)
  paper_executor.py  — single-position paper engine; INVARIANT-3
  runner.py          — atomic per-bar tick (used by replay/paper/live)
  replay.py          — bar-by-bar replay using PaperExecutor — the GATE
  paper_loop.py      — multi-strategy paper loop with heartbeat
  risk_engine.py     — PositionSizer + LiveRiskTracker (persisted)
  journal.py         — append-only audit trail writer
  mt5_account.py     — bridge-backed account/symbol metadata client
  live_executor.py   — 7 pre-flight gates before any real order
  parity_gate.py     — replay-parity-recency check for live #5
  ftmo_simulator.py  — Monte-Carlo bootstrap pass-rate

strategies/          — 11 strategies, ALL replay-parity tested
  vol_breakout.py    — survivor on US100/GER40/USDJPY D1
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
  app.py             — multi-page entry, sidebar, navigation
  pages/
    1_📊_Backtest.py        — single-strategy backtest + sweep + Promote
    2_🔬_Strategy_Studio.py — A/B + param sweep + walk-forward + wizard
    3_📡_Paper_Live.py      — Replay (parity) | Paper | Live (gated) | MT5 parity
    4_📈_Performance.py     — combined journal + analytics + editable notes
    5_⚙️_Account_Risk.py   — FTMO progress + EMERGENCY_STOP + sim
    6_💾_Data_Manager.py    — freshness + refresh + gap analysis
  components/        — reusable widgets (freshness, charts, forms, ...)

scripts/
  refresh_all_data.py   — refresh ALL parquets in one shot
  screen_strategy.py    — `make screen STRATEGY=<name>` — vet a new strategy
  ftmo_sim.py           — `make ftmo-sim` — pass-rate Monte-Carlo
  strategy_to_mql5.py   — Python → MQL5 EA scaffold
  mt5_parity_check.py   — Python backtest vs MT5 Strategy Tester CSV

docs/
  RUNBOOK.md            — operational procedures (FTMO test, going live, ...)
  index_edge_findings.md — current 6 vol_breakout survivors
```

## R&D loop

The strategy discovery loop is now a 60-second workflow:

```bash
# 1. Wizard creates strategies/<name>.py from a category template
#    (Page 2 in the dashboard, or hand-edit the template directly)
$EDITOR strategies/my_idea.py        # implement signals()

# 2. Vet it
make screen STRATEGY=my_idea          # → docs/my_idea_screening.md
                                       #   verdict: ADD / DISCARD / NEEDS_TUNING

# 3. If ADD: replay-parity test + journal in v2.db, then go live via Page 3.
```

## FTMO pass-rate simulator

```bash
make ftmo-sim                          # 30-day Phase 1 prob, ~30s for 10k iter
make ftmo-sim ARGS="--iterations 50000 --target 5"   # Phase 2 sim
```

Output includes per-strategy contribution to expected return AND to drawdown,
so you can answer "would adding/removing X lift my pass rate?".

## MT5 Strategy Tester parity

Before going live for a strategy:

```bash
python scripts/strategy_to_mql5.py --strategy vol_breakout
# 1. Open mql5/VolBreakout.mq5 in MetaEditor
# 2. Translate the OnTick body (Python source is comment-embedded)
# 3. Compile (F7), drop on chart matching ticker/TF/date range
# 4. Run Strategy Tester ("Every tick based on real ticks")
# 5. Right-click report → Save → CSV
#    → data/mt5_tester_reports/vol_breakout_US100.cash_D1.csv
python scripts/mt5_parity_check.py --strategy vol_breakout \
                                    --ticker US100.cash --tf D1
# → docs/parity_vol_breakout_US100.cash_D1.md
#    Verdict: PASS (count Δ ≤5%, P&L Δ ≤10%, per-trade match ≥80%)
```

The parity gate (live_executor pre-flight #5) ensures live orders are blocked
unless the strategy has a recent passing parity within 24 hours.

## Safety

The following CANNOT be bypassed except by explicit, typed acknowledgement:

- `data/EMERGENCY_STOP` file — touch from any process to refuse all live orders
- Time-guard rules (weekend_flat / daily_close_flat) — non-overridable in live mode
- Idempotency dedup window — same key within 1h is denied (timeout-retry safe)
- Daily loss cap and max_consecutive_losses — arithmetic facts, not preferences

The "I understand FTMO test risk" checkbox flips ONLY the parity-recency gate.
It does NOT bypass any of the above.

See `docs/RUNBOOK.md` for the operational procedure.
