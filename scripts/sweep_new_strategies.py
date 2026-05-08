#!/usr/bin/env python3
"""
sweep_new_strategies.py — backtest the 4 new strategies on the same
tickers/TFs the catalog uses, cost-priced, 60/40 split, hard-gate reported.

Strategies tested:
  - vol_break              (donchian + realized-vol filter)
  - orb_vol_filtered       (open-range breakout + vol filter)
  - vwap_fade              (session-anchored mean reversion)
  - trend_pullback_regime  (pullback to fast EMA, regime-gated)

Anti-curve-fitting discipline:
  - SAME params across all (ticker, tf) cells per strategy
  - 60/40 train/test, only OOS metrics reported
  - Cost defaults from core.cost_defaults ($4 commission, 0.05×ATR slip)
  - Hard gates applied: PF≥1.05, OOS PF≥1.0, n_test≥15

Output:
  - Console table sorted by Edge Score (desc)
  - docs/new_strategies_sweep_<timestamp>.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from core import cost_defaults as _cd
from core import edge_score as es
from core.backtest import partition_train_test, run_backtest
from core.data import load_parquet
from strategies.orb_vol_filtered import OrbVolFiltered, OrbVolFilteredParams
from strategies.trend_pullback_regime import (
    TrendPullbackRegime,
    TrendPullbackRegimeParams,
)
from strategies.vol_break import VolBreak, VolBreakParams
from strategies.vwap_fade import VwapFade, VwapFadeParams


# Ticker × TF cells we want to test. Pick from tickers we already have parquets
# for AND that appear in the existing catalog top-20 — they're the liquid,
# tradable instruments where edge actually has a chance.
CELLS: list[tuple[str, str, float]] = [
    # (ticker, tf, money_per_unit_per_lot)
    ("EURUSD",     "M15", 100_000.0),
    ("EURUSD",     "H1",  100_000.0),
    ("XAUUSD",     "M15", 100.0),
    ("XAUUSD",     "H1",  100.0),
    ("US100.cash", "M15", 1.0),
    ("US100.cash", "H1",  1.0),
    ("US30.cash",  "M15", 1.0),
    ("US30.cash",  "H1",  1.0),
    ("US500.cash", "M15", 1.0),
    ("JP225.cash", "M15", 1.0),
    ("JP225.cash", "H1",  1.0),
    ("HK50.cash",  "M15", 1.0),
    ("UK100.cash", "H1",  1.0),
    ("FRA40.cash", "H1",  1.0),
    ("GER40.cash", "H1",  1.0),
]


# Default lots per ticker — chosen so 1 ATR ≈ $200-500 risk on $100k account
LOTS_BY_TICKER: dict[str, float] = {
    "EURUSD":     1.0,
    "XAUUSD":     0.30,
    "US100.cash": 6.5,
    "US30.cash":  3.0,
    "US500.cash": 5.0,
    "JP225.cash": 2.0,
    "HK50.cash":  3.0,
    "UK100.cash": 4.0,
    "FRA40.cash": 4.0,
    "GER40.cash": 3.0,
}


def build_strategies(long_only: bool = False):
    """Return [(name, instance), ...] for the 4 new strategies.
    Same params on every cell — no per-ticker tuning."""
    return [
        ("vol_break_20",
         VolBreak(VolBreakParams(period=20, long_only=long_only))),
        ("orb_vol_filtered",
         OrbVolFiltered(OrbVolFilteredParams(long_only=long_only))),
        ("vwap_fade",
         VwapFade(VwapFadeParams(long_only=long_only))),
        ("trend_pullback_regime",
         TrendPullbackRegime(TrendPullbackRegimeParams(long_only=long_only))),
    ]


def parquet_path(ticker: str, tf: str) -> Path:
    return ROOT / "data" / f"{ticker}_{tf}.parquet"


def edge_score_for(test_pf, test_r, n_test, train_pf, train_r,
                     win_rate_pct, rr_ratio, recovery_days, max_dd_pct):
    """Build an EdgeStat and score it via core.edge_score so the score
    matches everything else in the platform. Only the fields edge_score
    actually reads are populated; the rest get dataclass defaults."""
    from core import edge_catalog
    deploy_safe = (test_pf >= 1.0 and train_pf >= 1.05
                     and n_test >= 15
                     and (recovery_days is None or recovery_days <= 90))
    try:
        e = edge_catalog.EdgeStat(
            ticker="x", tf="M15", strategy="x",
            n_train=int(n_test * 60 / 40),  # rough approx for compute
            train_pf=float(train_pf),
            train_r=float(train_r),
            n_test=int(n_test),
            test_pf=float(test_pf),
            test_r=float(test_r),
            win_rate_pct=float(win_rate_pct),
            rr_ratio=float(rr_ratio),
            max_dd_pct=float(max_dd_pct),
            recovery_days=float(recovery_days)
                if recovery_days is not None else None,
        )
        b = es.compute_from_edge_stat(e)
        return b.total, deploy_safe
    except Exception:
        return 0.0, deploy_safe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--commission", type=float,
                       default=_cd.DEFAULT_COMMISSION_USD)
    ap.add_argument("--slippage-atr", type=float,
                       default=_cd.DEFAULT_SLIPPAGE_ATR_FRAC)
    ap.add_argument("--balance", type=float,
                       default=_cd.DEFAULT_STARTING_BALANCE_USD)
    ap.add_argument("--train-pct", type=float, default=0.60)
    args = ap.parse_args()

    strategies = build_strategies(long_only=args.long_only)
    rows = []
    for ticker, tf, mpu in CELLS:
        ppath = parquet_path(ticker, tf)
        if not ppath.exists():
            print(f"  skip {ticker} {tf}: no parquet", file=sys.stderr)
            continue
        candles = load_parquet(ppath)
        if len(candles) < 500:
            print(f"  skip {ticker} {tf}: only {len(candles)} bars",
                    file=sys.stderr)
            continue
        lots = LOTS_BY_TICKER.get(ticker, 1.0)
        for sname, strat in strategies:
            try:
                sigs = strat.signals(candles)
                if not sigs:
                    rows.append((sname, ticker, tf, 0, 0.0, 0.0, 0.0, 0.0,
                                  0.0, 0.0, 0.0, False, "no_signals"))
                    continue
                res = run_backtest(
                    candles, sigs,
                    starting_balance=args.balance,
                    lots=lots,
                    money_per_unit_price=mpu,
                    commission_per_trade=args.commission,
                    slippage_per_fill_atr_frac=args.slippage_atr,
                )
                # Manual 60/40 split by entry bar — partition_train_test
                # returns aggregated metrics, but we need the trade lists
                # to compute R, win rate, R:R, etc. ourselves.
                n_bars = len(candles)
                split_idx = int(n_bars * args.train_pct)
                tr_trades = [t for t in res.trades
                                if t.entry_bar_idx < split_idx]
                te_trades = [t for t in res.trades
                                if t.entry_bar_idx >= split_idx]
                if not te_trades or not tr_trades:
                    rows.append((sname, ticker, tf, len(res.trades),
                                  0.0, 0.0, 0.0, 0.0,
                                  0.0, 0.0, 0.0, False,
                                  "thin_after_split"))
                    continue
                wins_tr = [t for t in tr_trades if t.realized_pnl > 0]
                losses_tr = [t for t in tr_trades if t.realized_pnl < 0]
                wins_te = [t for t in te_trades if t.realized_pnl > 0]
                losses_te = [t for t in te_trades if t.realized_pnl < 0]
                gp_tr = sum(t.realized_pnl for t in wins_tr)
                gl_tr = -sum(t.realized_pnl for t in losses_tr)
                gp_te = sum(t.realized_pnl for t in wins_te)
                gl_te = -sum(t.realized_pnl for t in losses_te)
                train_pf = (gp_tr / gl_tr) if gl_tr > 0 else (
                    9.99 if gp_tr > 0 else 0.0)
                test_pf = (gp_te / gl_te) if gl_te > 0 else (
                    9.99 if gp_te > 0 else 0.0)

                def _R(t):
                    risk = abs(t.entry_price - t.stop_price) * t.lots * mpu
                    return (t.realized_pnl / risk) if risk > 0 else 0.0

                train_R = (sum(_R(t) for t in tr_trades) / len(tr_trades)
                              if tr_trades else 0.0)
                test_R = (sum(_R(t) for t in te_trades) / len(te_trades)
                             if te_trades else 0.0)
                wr_te = (len(wins_te) / len(te_trades) * 100
                            if te_trades else 0.0)
                avg_win = (sum(_R(t) for t in wins_te) / len(wins_te)
                              if wins_te else 0.0)
                avg_loss = (-sum(_R(t) for t in losses_te) / len(losses_te)
                               if losses_te else 0.0)
                rr_te = (avg_win / avg_loss) if avg_loss > 0 else 0.0

                # Equity DD on TEST slice (rebuilt from te_trades cumsum)
                if te_trades:
                    eq = [args.balance]
                    for t in te_trades:
                        eq.append(eq[-1] + t.realized_pnl)
                    eq_arr = pd.Series(eq)
                    peak = eq_arr.cummax()
                    dd_pct = float(((peak - eq_arr) / peak).max() * 100)
                else:
                    dd_pct = 0.0
                # Recovery: bars from trough back to peak, → days using
                # candles[time] for the corresponding exit_bar_idx.
                if te_trades and dd_pct > 0.1:
                    eq_vals = pd.Series(eq).values
                    pk_vals = pd.Series(eq).cummax().values
                    trough_idx = int((pk_vals - eq_vals).argmax())
                    peak_value = pk_vals[trough_idx]
                    rec_idx = None
                    for k in range(trough_idx, len(eq_vals)):
                        if eq_vals[k] >= peak_value:
                            rec_idx = k
                            break
                    if rec_idx is None or trough_idx == 0:
                        rec_days = 999.0 if rec_idx is None else 0.0
                    else:
                        # eq[k] corresponds to te_trades[k-1].exit_bar_idx
                        i_tr = te_trades[trough_idx - 1].exit_bar_idx
                        i_re = te_trades[
                            min(rec_idx - 1, len(te_trades) - 1)
                        ].exit_bar_idx
                        try:
                            t_tr = pd.to_datetime(
                                candles["time"].iloc[i_tr])
                            t_re = pd.to_datetime(
                                candles["time"].iloc[i_re])
                            rec_days = max(
                                0.0,
                                (t_re - t_tr).total_seconds() / 86400)
                        except Exception:
                            rec_days = float(i_re - i_tr) / 24.0
                else:
                    rec_days = 0.0
                score, safe = edge_score_for(
                    test_pf, test_R, len(te_trades),
                    train_pf, train_R, wr_te, rr_te,
                    rec_days, dd_pct / 100,
                )
                rows.append((sname, ticker, tf, len(te_trades),
                              train_pf, test_pf, train_R, test_R,
                              wr_te, rr_te, rec_days, safe, "ok",
                              dd_pct, score))
            except Exception as ex:
                rows.append((sname, ticker, tf, 0, 0.0, 0.0, 0.0, 0.0,
                              0.0, 0.0, 0.0, False, f"err:{ex}"))

    # Sort by score desc — but those without score have 12 fields, those with have 14
    enriched = []
    for r in rows:
        if len(r) >= 15:
            enriched.append(r)
        else:
            # pad with zeros for sorting
            sname, ticker, tf, n = r[0], r[1], r[2], r[3]
            note = r[-1]
            enriched.append((sname, ticker, tf, n, 0, 0, 0, 0,
                              0, 0, 0, False, note, 0.0, 0.0))
    enriched.sort(key=lambda r: -r[14])

    # Pretty print
    print()
    print("=" * 130)
    print(f"{'Strategy':22} {'Ticker':12} {'TF':4} {'n_te':>4} "
          f"{'PF_tr':>5} {'PF_te':>5} {'R_te':>6} {'WR%':>4} {'R:R':>5} "
          f"{'recD':>5} {'DD%':>5} {'safe':>4} {'Score':>5}  note")
    print("=" * 130)
    for r in enriched:
        sname, ticker, tf, n, pf_tr, pf_te, _, R_te, wr, rr, rec, safe, \
            note, dd, sc = r
        rec_s = f"{rec:>4.0f}d" if rec < 900 else "never"
        safe_s = "✓" if safe else "✗"
        print(f"{sname[:22]:22} {ticker[:12]:12} {tf:4} {n:>4} "
              f"{pf_tr:>5.2f} {pf_te:>5.2f} {R_te:>+6.2f} {wr:>3.0f}% "
              f"{rr:>5.2f} {rec_s:>5} {dd:>4.1f}% {safe_s:>4} "
              f"{sc:>5.1f}  {note}")

    # Markdown output
    out = ROOT / "docs" / f"new_strategies_sweep_{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.md"
    with open(out, "w") as f:
        f.write(f"# New strategies sweep — {dt.datetime.utcnow().isoformat()}Z\n\n")
        f.write(f"Cost: ${args.commission}/trade + {args.slippage_atr}×ATR slip · "
                f"split {args.train_pct:.0%}/{1-args.train_pct:.0%}\n\n")
        f.write("| Strategy | Ticker | TF | n_test | PF_train | PF_test | R_test | WR% | R:R | recD | DD% | safe | Score | note |\n")
        f.write("|----------|--------|----|-------:|---------:|--------:|-------:|----:|----:|-----:|----:|:---:|------:|------|\n")
        for r in enriched:
            sname, ticker, tf, n, pf_tr, pf_te, _, R_te, wr, rr, rec, safe, \
                note, dd, sc = r
            rec_s = f"{rec:.0f}d" if rec < 900 else "never"
            safe_s = "✓" if safe else "✗"
            f.write(f"| {sname} | {ticker} | {tf} | {n} | {pf_tr:.2f} | "
                    f"{pf_te:.2f} | {R_te:+.2f} | {wr:.0f}% | {rr:.2f} | "
                    f"{rec_s} | {dd:.1f}% | {safe_s} | {sc:.1f} | {note} |\n")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
