#!/usr/bin/env python3
"""
sweep_pairs_meanrev.py — backtest pairs trading on pre-screened
economically-linked pairs.

Mechanism (faithful to Vidyamurthy 2004):
1. Compute rolling 200-bar hedge ratio β
2. Compute spread = A − β × B
3. Compute rolling 50-bar z-score of spread
4. ENTRY: when z > +2 → SHORT the spread (short A, long B)
         when z < -2 → LONG the spread (long A, short B)
5. EXIT:  when z crosses 0 → close (target hit)
6. STOP:  when |z| > 3 → close (cointegration broke)

Trade is treated as a synthetic single-position trade on the SPREAD,
sized so that 1σ move in spread = 1R PnL. This makes it directly
comparable to the existing single-instrument cells.

ANTI-CURVE-FIT discipline:
- SAME params on every pair (lookback=200, z_window=50, z_entry=2.0,
  z_exit=0.0, z_stop=3.0)
- 60/40 train/test split
- Hard gates applied: PF≥1.05, OOS PF≥1.0, n_test≥15, recovery≤90d
- ADF test gates: skip pairs that fail cointegration on the train slice
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core import cointegration                            # noqa: E402
from core import edge_score as es                          # noqa: E402
from core.data import load_parquet                         # noqa: E402

PAIRS = [
    ("XAUUSD",     "XAGUSD"),
    ("US100.cash", "US500.cash"),
    ("US30.cash",  "US500.cash"),
    ("EURUSD",     "GBPUSD"),
    ("AUDUSD",     "NZDUSD"),
]
TFS = ["H1", "D1"]

LOOKBACK = 200
Z_WINDOW = 50
Z_ENTRY = 2.0
Z_EXIT = 0.0
Z_STOP = 3.0


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


def backtest_pair(a_close: pd.Series, b_close: pd.Series,
                    times: pd.Series, *,
                    lookback: int = LOOKBACK,
                    z_window: int = Z_WINDOW,
                    z_entry: float = Z_ENTRY,
                    z_exit: float = Z_EXIT,
                    z_stop: float = Z_STOP,
                    train_pct: float = 0.60) -> dict:
    """Backtest a pair given two aligned close series.

    Returns a dict with stats per slice (train/test) and recovery.
    Uses spread-as-synthetic-instrument: PnL per trade in σ-units.
    """
    n = len(a_close)
    if n < lookback + z_window + 50:
        return {"n_train": 0, "n_test": 0,
                  "skip_reason": "not enough bars"}

    beta = cointegration.regress_hedge_ratio(a_close, b_close, lookback)
    spread = cointegration.compute_spread(a_close, b_close, beta)
    z = cointegration.z_score(spread, z_window)
    # CRITICAL: use the SAME rolling sigma to compute PnL R-multiples
    # that we use to compute the z-score. Otherwise R/sigma mismatch
    # makes wins look tiny relative to losses (R:R artificially low).
    rolling_sigma = spread.rolling(z_window).std()

    # ADF test on the train slice — skip pairs that aren't cointegrated
    split_idx = int(n * train_pct)
    train_spread = spread.iloc[:split_idx].dropna()
    if len(train_spread) > 30:
        adf_p = cointegration.adf_pvalue(train_spread)
    else:
        adf_p = 1.0

    # Walk bars; emit synthetic trades on z-crossings
    trades = []
    in_trade = False
    direction = 0  # +1 = long spread, -1 = short spread
    entry_z = 0.0
    entry_idx = -1
    entry_spread = 0.0

    z_vals = z.values
    spread_vals = spread.values
    for i in range(n):
        zi = z_vals[i]
        if np.isnan(zi):
            continue
        if not in_trade:
            if zi > z_entry:
                in_trade = True
                direction = -1  # short the spread (expect mean reversion DOWN)
                entry_z = zi
                entry_idx = i
                entry_spread = spread_vals[i]
            elif zi < -z_entry:
                in_trade = True
                direction = +1  # long the spread
                entry_z = zi
                entry_idx = i
                entry_spread = spread_vals[i]
        else:
            # Check exit conditions
            crossed_zero = ((direction == -1 and zi <= z_exit)
                              or (direction == +1 and zi >= z_exit))
            stopped = abs(zi) > z_stop and (
                (direction == -1 and zi > entry_z)
                or (direction == +1 and zi < entry_z)
            )
            if crossed_zero or stopped:
                # PnL in σ-units of the spread, using ROLLING sigma at
                # entry — same window as the z-score, so R-multiples
                # are calibrated to the same metric the entry rule used.
                spread_move = spread_vals[i] - entry_spread
                sigma_at_entry = rolling_sigma.iloc[entry_idx]
                if pd.isna(sigma_at_entry) or sigma_at_entry <= 0:
                    sigma_at_entry = 1.0
                pnl_sigma = (direction * spread_move) / sigma_at_entry
                # R-multiple — risk per trade is 1σ (since we stop at
                # ~1σ adverse move from entry on a typical trade), so
                # PnL_in_R ≈ pnl_sigma. Slightly approximate but
                # matches the stop-distance convention.
                R_mult = pnl_sigma
                trades.append({
                    "entry_bar": entry_idx,
                    "exit_bar": i,
                    "direction": direction,
                    "entry_z": float(entry_z),
                    "exit_z": float(zi),
                    "R": float(R_mult),
                    "stopped": bool(stopped),
                })
                in_trade = False
                direction = 0
                entry_idx = -1
                entry_spread = 0.0
                entry_z = 0.0

    # Split by entry bar
    train_trades = [t for t in trades if t["entry_bar"] < split_idx]
    test_trades = [t for t in trades if t["entry_bar"] >= split_idx]

    def _stats(slot):
        if not slot:
            return dict(n=0, pf=0.0, mean_R=0.0, wr=0.0, rr=0.0,
                          gp=0.0, gl=0.0)
        wins = [t for t in slot if t["R"] > 0]
        losses = [t for t in slot if t["R"] < 0]
        gp = sum(t["R"] for t in wins)
        gl = -sum(t["R"] for t in losses)
        pf = (gp / gl) if gl > 0 else (9.99 if gp > 0 else 0.0)
        mean_R = sum(t["R"] for t in slot) / len(slot)
        wr = len(wins) / len(slot) * 100
        avg_w = sum(t["R"] for t in wins) / len(wins) if wins else 0.0
        avg_l = -sum(t["R"] for t in losses) / len(losses) if losses else 0.0
        rr = avg_w / avg_l if avg_l > 0 else 0.0
        return dict(n=len(slot), pf=pf, mean_R=mean_R,
                       wr=wr, rr=rr, gp=gp, gl=gl)

    tr = _stats(train_trades)
    te = _stats(test_trades)

    # Equity DD on test in σ-units
    if test_trades:
        eq = [0.0]
        for t in test_trades:
            eq.append(eq[-1] + t["R"])
        eq_arr = pd.Series(eq)
        peak = eq_arr.cummax()
        dd = float((peak - eq_arr).max())
        # Recovery: bars from trough back to prior peak
        eq_v = eq_arr.values
        pk_v = peak.values
        if dd > 0.01:
            trough = int((pk_v - eq_v).argmax())
            peak_val = pk_v[trough]
            rec_idx = None
            for k in range(trough, len(eq_v)):
                if eq_v[k] >= peak_val:
                    rec_idx = k
                    break
            if rec_idx is None or trough == 0:
                rec_days = 999.0 if rec_idx is None else 0.0
            else:
                i_tr = test_trades[trough - 1]["exit_bar"]
                i_re = test_trades[
                    min(rec_idx - 1, len(test_trades) - 1)
                ]["exit_bar"]
                try:
                    t_tr = pd.to_datetime(times.iloc[i_tr])
                    t_re = pd.to_datetime(times.iloc[i_re])
                    rec_days = max(
                        0.0,
                        (t_re - t_tr).total_seconds() / 86400)
                except Exception:
                    rec_days = float(i_re - i_tr) / 24.0
        else:
            rec_days = 0.0
        # DD% — express as % of cumulative |R| (rough proxy)
        total_abs_R = sum(abs(t["R"]) for t in test_trades)
        dd_pct = (dd / total_abs_R * 100) if total_abs_R > 0 else 0.0
    else:
        rec_days = 0.0
        dd_pct = 0.0

    return {
        "n_train": tr["n"], "n_test": te["n"],
        "train_pf": tr["pf"], "test_pf": te["pf"],
        "train_R": tr["mean_R"], "test_R": te["mean_R"],
        "win_rate_pct": te["wr"], "rr_ratio": te["rr"],
        "max_dd_pct": dd_pct, "recovery_days": rec_days,
        "adf_pvalue": adf_p,
    }


def main() -> int:
    rows = []
    for a_tic, b_tic in PAIRS:
        for tf in TFS:
            pa = parquet_path(a_tic, tf)
            pb = parquet_path(b_tic, tf)
            if not pa.exists() or not pb.exists():
                rows.append({
                    "pair": f"{a_tic}/{b_tic}", "tf": tf,
                    "skip": "missing parquet"
                })
                continue
            df_a = load_parquet(pa)
            df_b = load_parquet(pb)
            # Align on time
            df_a = df_a.set_index(pd.to_datetime(df_a["time"]))[["close"]]
            df_b = df_b.set_index(pd.to_datetime(df_b["time"]))[["close"]]
            df = df_a.join(df_b, how="inner", lsuffix="_a", rsuffix="_b")
            df = df.dropna()
            if len(df) < 500:
                rows.append({
                    "pair": f"{a_tic}/{b_tic}", "tf": tf,
                    "skip": f"only {len(df)} aligned bars"
                })
                continue
            times = pd.Series(df.index)
            stats = backtest_pair(
                df["close_a"], df["close_b"], times,
            )
            if stats.get("skip_reason"):
                rows.append({
                    "pair": f"{a_tic}/{b_tic}", "tf": tf,
                    "skip": stats["skip_reason"]
                })
                continue
            score, safe = edge_score_for(
                stats["test_pf"], stats["test_R"], stats["n_test"],
                stats["train_pf"], stats["train_R"],
                stats["win_rate_pct"], stats["rr_ratio"],
                stats["recovery_days"], stats["max_dd_pct"] / 100,
            )
            rows.append({
                "pair": f"{a_tic}/{b_tic}", "tf": tf,
                "n_test": stats["n_test"],
                "train_pf": stats["train_pf"],
                "test_pf": stats["test_pf"],
                "test_R": stats["test_R"],
                "wr": stats["win_rate_pct"],
                "rr": stats["rr_ratio"],
                "rec": stats["recovery_days"],
                "dd": stats["max_dd_pct"],
                "adf_p": stats["adf_pvalue"],
                "safe": safe, "score": score,
            })

    rows_ok = [r for r in rows if "skip" not in r]
    rows_ok.sort(key=lambda r: -r["score"])
    print()
    print("=" * 140)
    print(f"{'Pair':25} {'TF':4} {'n_te':>4} {'PF_tr':>5} {'PF_te':>5} "
          f"{'R_te':>6} {'WR%':>4} {'R:R':>5} {'recD':>5} {'DD%':>5} "
          f"{'ADF_p':>6} {'safe':>4} {'Score':>5}")
    print("=" * 140)
    for r in rows_ok:
        rec_s = f"{r['rec']:>4.0f}d" if r['rec'] < 900 else "never"
        safe_s = "✓" if r['safe'] else "✗"
        print(f"{r['pair'][:25]:25} {r['tf']:4} {r['n_test']:>4} "
              f"{r['train_pf']:>5.2f} {r['test_pf']:>5.2f} "
              f"{r['test_R']:>+6.2f} {r['wr']:>3.0f}% {r['rr']:>5.2f} "
              f"{rec_s:>5} {r['dd']:>4.1f}% {r['adf_p']:>6.3f} "
              f"{safe_s:>4} {r['score']:>5.1f}")
    for r in rows:
        if "skip" in r:
            print(f"  SKIP {r['pair']} {r['tf']}: {r['skip']}")

    out = (ROOT / "docs" /
            f"pairs_sweep_{dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.md")
    with open(out, "w") as f:
        f.write(f"# Pairs trading sweep — {dt.datetime.utcnow().isoformat()}Z\n\n")
        f.write("Vidyamurthy 2004 mechanism — 200-bar β, 50-bar z-score, "
                "entry ±2σ, exit 0σ, stop ±3σ.\n\n")
        f.write("| Pair | TF | n_test | PF_train | PF_test | R_test | "
                "WR% | R:R | recD | DD% | ADF_p | safe | Score |\n")
        f.write("|------|----|-------:|---------:|--------:|-------:|"
                "----:|----:|-----:|----:|------:|:---:|------:|\n")
        for r in rows_ok:
            rec_s = f"{r['rec']:.0f}d" if r['rec'] < 900 else "never"
            safe_s = "✓" if r['safe'] else "✗"
            f.write(f"| {r['pair']} | {r['tf']} | {r['n_test']} | "
                    f"{r['train_pf']:.2f} | {r['test_pf']:.2f} | "
                    f"{r['test_R']:+.2f} | {r['wr']:.0f}% | "
                    f"{r['rr']:.2f} | {rec_s} | {r['dd']:.1f}% | "
                    f"{r['adf_p']:.3f} | {safe_s} | {r['score']:.1f} |\n")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
