#!/usr/bin/env python3
"""
sweep_tsmom.py — sweep AQR-style time-series momentum across the
catalog tickers, with TF-appropriate "12-month / 1-month" parameters.

ANTI-CURVE-FIT discipline:
- Same per-TF params on EVERY ticker. No per-cell tuning.
- Cost defaults from core.cost_defaults ($4 + 0.05×ATR slip).
- 60/40 train/test split. Only OOS reported.
- Hard gates applied (PF≥1.05, OOS PF≥1.0, n_test≥15, recovery≤90d).

TF-appropriate windows (faithful to AQR's "12-month signal, 1-month hold"):
- D1:  lookback=252,  hold=21    (12 months trading days, 1 month)
- H1:  lookback=6048, hold=504   (12mo × 21d × 24h, 1mo × 21d × 24h)
- M15: SKIPPED — 12-month TSMOM doesn't fit a 15-minute holding period
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from core import cost_defaults as _cd                              # noqa: E402
from core import edge_score as es                                  # noqa: E402
from core.backtest import run_backtest                              # noqa: E402
from core.data import load_parquet                                  # noqa: E402
from strategies.tsmom import Tsmom, TsmomParams                     # noqa: E402


# Same ticker × mpu mapping as sweep_new_strategies.py
CELLS_BY_TF: dict[str, list[tuple[str, float]]] = {
    "D1": [
        ("EURUSD", 100_000.0),
        ("GBPUSD", 100_000.0),
        ("USDJPY", 700.0),
        ("AUDUSD", 100_000.0),
        ("XAUUSD", 100.0),
        ("XAGUSD", 5_000.0),
        ("US100.cash", 1.0),
        ("US30.cash",  1.0),
        ("US500.cash", 1.0),
        ("JP225.cash", 1.0),
        ("HK50.cash",  1.0),
        ("UK100.cash", 1.0),
        ("FRA40.cash", 1.0),
        ("GER40.cash", 1.0),
    ],
    "H1": [
        ("EURUSD", 100_000.0),
        ("GBPUSD", 100_000.0),
        ("XAUUSD", 100.0),
        ("US100.cash", 1.0),
        ("US30.cash",  1.0),
        ("US500.cash", 1.0),
        ("JP225.cash", 1.0),
        ("UK100.cash", 1.0),
        ("FRA40.cash", 1.0),
        ("GER40.cash", 1.0),
    ],
}


# Same lots map as catalog (so backtest costs are consistent)
LOTS_BY_TICKER: dict[str, float] = {
    "EURUSD":     1.0,
    "GBPUSD":     1.0,
    "USDJPY":     1.0,
    "AUDUSD":     1.5,
    "XAUUSD":     0.30,
    "XAGUSD":     0.40,
    "US100.cash": 6.5,
    "US30.cash":  3.0,
    "US500.cash": 5.0,
    "JP225.cash": 2.0,
    "HK50.cash":  3.0,
    "UK100.cash": 4.0,
    "FRA40.cash": 4.0,
    "GER40.cash": 3.0,
}


# AQR-default params per TF — these are anti-curve-fit by construction
PARAMS_BY_TF: dict[str, TsmomParams] = {
    # 252 trading days = ~12 months. 21 = ~1 month. AQR's published default.
    "D1":  TsmomParams(lookback_bars=252,  hold_bars=21),
    # 12 months × ~21 trading days × 24h = 6048 bars.
    # 1 month × 21 days × 24h = 504 bars.
    "H1":  TsmomParams(lookback_bars=6048, hold_bars=504),
}


def parquet_path(ticker: str, tf: str) -> Path:
    return ROOT / "data" / f"{ticker}_{tf}.parquet"


def edge_score_for(test_pf, test_r, n_test, train_pf, train_r,
                     win_rate_pct, rr_ratio, recovery_days, max_dd_pct):
    from core import edge_catalog
    deploy_safe = (test_pf >= 1.0 and train_pf >= 1.05
                     and n_test >= 15
                     and (recovery_days is None or recovery_days <= 90))
    try:
        e = edge_catalog.EdgeStat(
            ticker="x", tf="D1", strategy="x",
            n_train=int(n_test * 60 / 40),
            train_pf=float(train_pf), train_r=float(train_r),
            n_test=int(n_test),
            test_pf=float(test_pf), test_r=float(test_r),
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

    rows = []
    for tf, cells in CELLS_BY_TF.items():
        params = PARAMS_BY_TF[tf]
        if args.long_only:
            params = TsmomParams(
                lookback_bars=params.lookback_bars,
                hold_bars=params.hold_bars,
                long_only=True,
            )
        for ticker, mpu in cells:
            ppath = parquet_path(ticker, tf)
            if not ppath.exists():
                print(f"  skip {ticker} {tf}: no parquet", file=sys.stderr)
                continue
            candles = load_parquet(ppath)
            if len(candles) < params.lookback_bars + 200:
                print(f"  skip {ticker} {tf}: only {len(candles)} bars "
                      f"(need {params.lookback_bars + 200})",
                      file=sys.stderr)
                continue
            lots = LOTS_BY_TICKER.get(ticker, 1.0)
            try:
                strat = Tsmom(params)
                sigs = strat.signals(candles)
                if not sigs:
                    rows.append((ticker, tf, 0, 0.0, 0.0, 0.0, 0.0,
                                  0.0, 0.0, 0.0, False, "no_signals",
                                  0.0, 0.0))
                    continue
                res = run_backtest(
                    candles, sigs,
                    starting_balance=args.balance,
                    lots=lots,
                    money_per_unit_price=mpu,
                    commission_per_trade=args.commission,
                    slippage_per_fill_atr_frac=args.slippage_atr,
                )
                # 60/40 split by entry bar
                n_bars = len(candles)
                split_idx = int(n_bars * args.train_pct)
                tr_trades = [t for t in res.trades
                                if t.entry_bar_idx < split_idx]
                te_trades = [t for t in res.trades
                                if t.entry_bar_idx >= split_idx]
                if not te_trades or not tr_trades:
                    rows.append((ticker, tf, len(res.trades),
                                  0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                  False, "thin_after_split", 0.0, 0.0))
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

                # Equity DD on TEST
                eq = [args.balance]
                for t in te_trades:
                    eq.append(eq[-1] + t.realized_pnl)
                eq_arr = pd.Series(eq)
                peak = eq_arr.cummax()
                dd_pct = float(((peak - eq_arr) / peak).max() * 100)
                # Recovery
                if dd_pct > 0.1:
                    eq_vals = eq_arr.values
                    pk_vals = peak.values
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
                rows.append((ticker, tf, len(te_trades),
                              train_pf, test_pf, train_R, test_R,
                              wr_te, rr_te, rec_days, safe, "ok",
                              dd_pct, score))
            except Exception as ex:
                rows.append((ticker, tf, 0, 0.0, 0.0, 0.0, 0.0,
                              0.0, 0.0, 0.0, False, f"err:{ex}", 0.0, 0.0))

    rows.sort(key=lambda r: -r[13])

    print()
    print("=" * 122)
    print(f"{'Ticker':12} {'TF':4} {'n_te':>4} {'PF_tr':>5} {'PF_te':>5} "
          f"{'R_te':>6} {'WR%':>4} {'R:R':>5} {'recD':>5} {'DD%':>5} "
          f"{'safe':>4} {'Score':>5}  note")
    print("=" * 122)
    for r in rows:
        ticker, tf, n, pf_tr, pf_te, _, R_te, wr, rr, rec, safe, note, dd, sc = r
        rec_s = f"{rec:>4.0f}d" if rec < 900 else "never"
        safe_s = "✓" if safe else "✗"
        print(f"{ticker[:12]:12} {tf:4} {n:>4} {pf_tr:>5.2f} {pf_te:>5.2f} "
              f"{R_te:>+6.2f} {wr:>3.0f}% {rr:>5.2f} {rec_s:>5} "
              f"{dd:>4.1f}% {safe_s:>4} {sc:>5.1f}  {note}")

    out = (ROOT / "docs" /
            f"tsmom_sweep_{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.md")
    with open(out, "w") as f:
        f.write(f"# TSMOM sweep — {dt.datetime.utcnow().isoformat()}Z\n\n")
        f.write(f"AQR Moskowitz/Ooi/Pedersen 2012 mechanism. Cost: "
                f"${args.commission}/trade + {args.slippage_atr}×ATR slip · "
                f"split {args.train_pct:.0%}/{1-args.train_pct:.0%}\n\n")
        f.write("Per-TF params (anti-curve-fit, no per-cell tuning):\n")
        for tf, p in PARAMS_BY_TF.items():
            f.write(f"- **{tf}**: lookback={p.lookback_bars}, "
                    f"hold={p.hold_bars}\n")
        f.write("\n")
        f.write("| Ticker | TF | n_test | PF_train | PF_test | R_test | WR% | R:R | recD | DD% | safe | Score | note |\n")
        f.write("|--------|----|-------:|---------:|--------:|-------:|----:|----:|-----:|----:|:---:|------:|------|\n")
        for r in rows:
            ticker, tf, n, pf_tr, pf_te, _, R_te, wr, rr, rec, safe, \
                note, dd, sc = r
            rec_s = f"{rec:.0f}d" if rec < 900 else "never"
            safe_s = "✓" if safe else "✗"
            f.write(f"| {ticker} | {tf} | {n} | {pf_tr:.2f} | {pf_te:.2f} | "
                    f"{R_te:+.2f} | {wr:.0f}% | {rr:.2f} | {rec_s} | "
                    f"{dd:.1f}% | {safe_s} | {sc:.1f} | {note} |\n")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
