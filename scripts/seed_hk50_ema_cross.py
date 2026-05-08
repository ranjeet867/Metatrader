#!/usr/bin/env python3
"""seed_hk50_ema_cross.py — seed v2.db + create paper deployment for
the HK50.cash M15 ema_cross_12_26 RR_1:2 long-only cell.

Backtest results (cost-priced, 60/40 OOS):
  - PF_train 1.34 / PF_test 1.76
  - mean_R_test +0.43, WR 50%
  - 28.1% DD, recovers in 32 days
  - Net P&L $+122k over ~6 months at 0.30% risk (compounding)

Deploys as PAPER with conservative 0.05% risk + $50 max-$ cap so the
DD floor is tame even if the backtest oversold the edge.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import account_manager, cost_defaults, storage   # noqa: E402
from core import deployment as dep_mod   # noqa: E402
from core.backtest import run_backtest   # noqa: E402
from core.data import load_parquet   # noqa: E402
from core.deployment import Deployment   # noqa: E402
from core.symbol_info_loader import try_load   # noqa: E402

from strategies.ema_cross import EmaCross, EmaCrossParams   # noqa: E402


SYMBOL = "HK50.cash"
TF = "M15"
PARAMS = EmaCrossParams(
    fast_period=12, slow_period=26, atr_period=14,
    stop_atr_mult=1.5, target_atr_mult=3.0, long_only=True,
)


def _seed_v2db() -> str:
    """Run the backtest at 0.30% risk (the verification config) and
    persist to v2.db so the catalog and rebaseline see this cell."""
    db_path = ROOT / "data" / "v2.db"
    storage.init_schema(db_path)
    df = load_parquet(ROOT / "data" / f"{SYMBOL}_{TF}.parquet")
    sym_info = try_load(SYMBOL)
    strat = EmaCross(PARAMS)
    sigs = strat.signals(df)
    res = run_backtest(
        df, sigs, starting_balance=100_000.0, lots=0.0,
        money_per_unit_price=1.0,
        commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        risk_pct=0.30, symbol_info=sym_info,
        symbol=SYMBOL,
    )
    n = len(res.trades)
    pnl = sum(t.realized_pnl for t in res.trades)
    if n == 0:
        raise RuntimeError("no trades — sanity-check failed")
    run_id = uuid.uuid4().hex[:12]
    now_iso = datetime.now(timezone.utc).isoformat()
    cfg = {f: getattr(PARAMS, f) for f in PARAMS.__dataclass_fields__.keys()}
    storage.save_run(
        db_path, run_id,
        started_at_utc=now_iso,
        symbol=SYMBOL, tf=TF,
        strategy_name="ema_cross_12_26",
        config_json=json.dumps(cfg, sort_keys=True),
        starting_balance=100_000.0,
    )
    trade_dicts = []
    for t in res.trades:
        trade_dicts.append({
            "symbol": SYMBOL,
            "direction": t.direction,
            "opened_at_utc": str(df["time"].iloc[t.entry_bar_idx]),
            "closed_at_utc": str(df["time"].iloc[t.exit_bar_idx]),
            "entry_price": t.entry_price,
            "stop_price": t.stop_price,
            "target_price": t.target_price,
            "exit_price": t.exit_price,
            "lots": t.lots,
            "realized_pnl": t.realized_pnl,
            "r_multiple": t.r_multiple,
            "close_reason": str(t.close_reason),
            "mode": "backtest",
            "strategy": "ema_cross_12_26",
            "tf": TF,
        })
    storage.save_trades(db_path, run_id, trade_dicts)
    storage.finish_run(
        db_path, run_id,
        finished_at_utc=now_iso,
        ending_equity=res.ending_balance,
        n_trades=n,
        sum_realized_pnl=res.sum_realized_pnl,
        equity_curve_pnl=res.equity_curve_pnl,
        reconciles=res.reconciles,
    )
    print(f"  ✅ v2.db row written: run_id={run_id}  n={n}  "
          f"pnl=${pnl:+,.0f}")
    return run_id


def _write_markdown_row():
    """Append a row to a new optimization markdown so edge_catalog
    surfaces this cell in the Library / Composer."""
    md_path = ROOT / "docs" / "optimization_2026-05-06_hk50_satellite.md"
    md_path.write_text("""# HK50 ema_cross_12_26 M15 — paper-tier addendum (2026-05-06)

- Run UTC: `2026-05-06T20:30:00+00:00`
- Strategy: `ema_cross_12_26` long-only, R:R 1:2 (stop 1.5×ATR, target 3.0×ATR)
- Methodology: cost-priced backtest, 60/40 OOS split, $4 commission + 0.05×ATR slippage
- Data window: 2025-10-23 → 2026-05-06 (~0.53 years M15 — LIMITED SAMPLE)
- Status: **PAPER ONLY** until 4 weeks of live observation confirms edge

This cell shows OOS PF 1.76 with mean_R +0.43 over 42 test trades, but the
data window is only 6 months (one regime). Train PF was lukewarm (1.34) →
test PF strong (1.76); could be genuine recent edge or regime tailwind that
won't persist. **DD floor is 28% at 0.30% risk** — well outside FTMO 10%
total-loss limit. Sizing in production: 0.05% risk + $50 max-$ cap → DD
floor ~4.7%, acceptable for paper / satellite tier.

## Cell

| rank | strategy | ticker | tf | side | R:R | n | PF | R | win% | netPnL$ | maxDD% | maxDD$ | DDdays | recovD | streak | rr | avgWin$ | avgLoss$ | CAGR | P(pass) | sus | score |
|---:|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `ema_cross_12_26` | `HK50.cash` | `M15` | long | 1:2 | 42 | 1.76 | +0.43 | 50 | +20,400 | 28.1 | 28,100 | 18 | 32 | 5 | 2.00 | +1,260 | -560 | +6.0% | 60% | ✅ | 19.0 |

**Why score 19.0 (not higher):**
- Limited sample (~0.53y) caps confidence
- 28% DD is borderline FTMO-deployable even at 0.05% risk
- Train regime weaker than test → potential mean-reversion of the edge
- HK50 = different session (Asia) → diversification adds when paired with US/EU cells

## Deployment

Created as PAPER with:
- `risk_pct=0.05`, `max_money_risk_usd=50`
- `daily_cap_pct=0.5` (single-trade-loss-tolerable)
- Run for 4 weeks paper → review live R vs catalog R (+0.43)
- Edge-decay autolearner will demote if rolling-30 R drops > 2σ below catalog
""")
    print(f"  ✅ Markdown catalog row written: {md_path.name}")


def _create_paper_deployment():
    accounts = account_manager.list_accounts()
    if not accounts:
        raise RuntimeError("no MT5 account configured")
    active = accounts[0]
    deployment_id = Deployment.slug("ema_cross_12_26", SYMBOL, TF)
    cfg = {f: getattr(PARAMS, f) for f in PARAMS.__dataclass_fields__.keys()}
    dep = Deployment(
        deployment_id=deployment_id,
        strategy="ema_cross_12_26",
        ticker=SYMBOL, tf=TF, long_only=True,
        params=cfg,
        risk_pct=0.05,                     # very conservative
        daily_cap_pct=0.5,
        max_money_risk_usd=50.0,           # hard $-cap per trade
        max_lots=15.0,
        status="paper",
        notes=("HK50 ema_cross_12_26 M15 RR_1:2 long — 4-week paper "
                "trial. OOS PF 1.76, mean_R +0.43, 28% DD. Conservative "
                "sizing (0.05% / $50 max-$). Watch live R vs +0.43; "
                "auto-demote at z<-2."),
    )
    dep_mod.upsert_deployment(active.login, dep)
    print(f"  ✅ Paper deployment created on account #{active.login}: "
          f"{deployment_id}")
    print(f"      risk_pct={dep.risk_pct}%  daily_cap={dep.daily_cap_pct}%  "
          f"max_$={dep.max_money_risk_usd}  status={dep.status}")


def main() -> int:
    print("Shipping HK50.cash M15 ema_cross_12_26 RR_1:2 long")
    print(f"Costs: ${cost_defaults.DEFAULT_COMMISSION_USD} commission, "
          f"{cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC} ATR slip\n")
    _seed_v2db()
    _write_markdown_row()
    _create_paper_deployment()
    print("\nDONE. Reload Operations + Strategy Library to see the new cell.")
    print("Runner picks it up on next 5s tick — watch Paper page for activity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
