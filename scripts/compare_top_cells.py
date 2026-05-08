#!/usr/bin/env python3
"""
compare_top_cells.py — single-view side-by-side comparison of the top
candidate strategy cells, with all the metrics that matter:

    R:R · WR · PF · trades · trades/mo · $/mo · max DD% · max consec L
    recovery days · OOS WR · OOS PF · survival score

Costs are realistic: $4 commission / round trip, 0.05×ATR slippage,
auto per-instrument tick value from data/symbol_info.json.

Usage:
    python scripts/compare_top_cells.py
    python scripts/compare_top_cells.py --html       # also write HTML view
    python scripts/compare_top_cells.py --csv path   # also write CSV
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sweep_rr_winrate import _backtest_one    # noqa: E402

# Curated comparison set — top survivors from the cost-priced sweep.
# Format: (display_label, strategy_name, ticker, tf, stop_mult, target_mult, long_only, group)
CELLS = [
    # ─ Gold (multi-RR for sweet-spot study) ──────────────────────────
    ("donchian_55 XAUUSD D1 R:R1.0", "donchian_55", "XAUUSD", "D1", 1.5, 1.5,  True, "GOLD"),
    ("donchian_55 XAUUSD D1 R:R1.1", "donchian_55", "XAUUSD", "D1", 1.5, 1.65, True, "GOLD"),
    ("donchian_55 XAUUSD D1 R:R1.5", "donchian_55", "XAUUSD", "D1", 1.5, 2.25, True, "GOLD"),
    ("donchian_55 XAUUSD D1 R:R2.0", "donchian_55", "XAUUSD", "D1", 1.5, 3.0,  True, "GOLD"),
    ("donchian_20 XAUUSD H1 R:R1.0", "donchian_20", "XAUUSD", "H1", 1.5, 1.5,  True, "GOLD-H1"),
    ("donchian_55 XAUUSD H1 R:R1.1", "donchian_55", "XAUUSD", "H1", 1.5, 1.65, True, "GOLD-H1"),

    # ─ Silver (correlation comparison vs gold) ───────────────────────
    ("donchian_55 XAGUSD D1 R:R1.0", "donchian_55", "XAGUSD", "D1", 1.5, 1.5,  True, "SILVER"),
    ("donchian_55 XAGUSD D1 R:R1.1", "donchian_55", "XAGUSD", "D1", 1.5, 1.65, True, "SILVER"),
    ("donchian_55 XAGUSD D1 R:R1.5", "donchian_55", "XAGUSD", "D1", 1.5, 2.25, True, "SILVER"),

    # ─ Palladium (3rd metal for the metals-correlation read) ─────────
    ("rsi_30_70 XPDUSD H1 R:R1.1", "rsi_30_70", "XPDUSD", "H1", 1.5, 1.65, True, "PALLAD."),

    # ─ US indices (your existing strength) ───────────────────────────
    ("donchian_55 US100 D1 R:R1.5", "donchian_55", "US100.cash", "D1", 1.5, 2.25, True, "US100"),
    ("donchian_20 US100 D1 R:R2.0", "donchian_20", "US100.cash", "D1", 1.5, 3.0,  True, "US100"),
    ("donchian_55 US100 D1 R:R1.1", "donchian_55", "US100.cash", "D1", 1.5, 1.65, True, "US100"),
    ("donchian_20 US30 D1 R:R1.0",  "donchian_20", "US30.cash",  "D1", 1.5, 1.5,  True, "US30"),

    # ─ FX ───────────────────────────────────────────────────────────
    ("ema_cross_12_26 GBPUSD D1 R:R1.5", "ema_cross_12_26", "GBPUSD", "D1", 1.5, 2.25, True, "FX"),
    ("rsi_30_70 EURUSD M15 R:R1.0",       "rsi_30_70",       "EURUSD", "M15", 1.5, 1.5,  True, "FX"),
]


# ANSI colour helpers — no extra dependency
def _c(text, code):
    return f"\033[{code}m{text}\033[0m"

GREEN = lambda t: _c(t, "32")
YELLOW = lambda t: _c(t, "33")
RED = lambda t: _c(t, "31")
BOLD = lambda t: _c(t, "1")
DIM = lambda t: _c(t, "2")


def _verdict(pf: float, score: float) -> str:
    if score > 15 and pf >= 1.5:
        return GREEN("🟢 deploy")
    if score > 0 and pf >= 1.1:
        return GREEN("🟢 ok")
    if score > -10 and pf >= 1.0:
        return YELLOW("🟡 marginal")
    return RED("🔴 skip")


def _pf_color(pf: float) -> str:
    if pf >= 1.5:
        return GREEN(f"{pf:.2f}")
    if pf >= 1.1:
        return YELLOW(f"{pf:.2f}")
    return RED(f"{pf:.2f}")


def _wr_color(wr: float) -> str:
    if wr >= 60:
        return GREEN(f"{wr:.0f}%")
    if wr >= 50:
        return YELLOW(f"{wr:.0f}%")
    return RED(f"{wr:.0f}%")


def _dd_color(dd: float) -> str:
    if dd <= 1.0:
        return GREEN(f"{dd:.2f}%")
    if dd <= 5.0:
        return YELLOW(f"{dd:.2f}%")
    return RED(f"{dd:.2f}%")


def _streak_color(s: int) -> str:
    if s <= 5:
        return GREEN(str(s))
    if s <= 10:
        return YELLOW(str(s))
    return RED(str(s))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None,
                     help="Also write CSV to this path")
    ap.add_argument("--html", action="store_true",
                     help="Also write HTML view to /tmp/cells_compare.html")
    ap.add_argument("--slip-atr-frac", type=float, default=0.05)
    ap.add_argument("--commission", type=float, default=4.0)
    ap.add_argument("--no-cost", action="store_true",
                     help="Override slip + commission to 0 for theoretical view")
    args = ap.parse_args()

    slip = 0.0 if args.no_cost else args.slip_atr_frac
    comm = 0.0 if args.no_cost else args.commission

    rows = []
    print(f"\nRe-testing {len(CELLS)} cells with "
          f"slippage={slip}×ATR · commission=${comm}/trade · "
          f"auto per-instrument tick value...\n")
    for i, (label, strat, ticker, tf, sm, tm, lo, group) in enumerate(CELLS):
        print(f"  [{i+1:2d}/{len(CELLS)}]  {label}", end="", flush=True)
        try:
            r = _backtest_one(
                strategy_name=strat, ticker=ticker, tf=tf,
                stop_mult=sm, target_mult=tm, long_only=lo,
                balance=91_400, lots=0.1, money_per_unit=1.0,
                train_pct=0.6, slip_atr_frac=slip,
                commission_per_trade=comm,
            )
            print("  ✓")
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}")
            r = None
        if r is None:
            continue
        # $/mo from total $ + span (compute span from data)
        from core.data import load_parquet
        df = load_parquet(f"data/{ticker}_{tf}.parquet")
        span_mo = max(1.0, (df["time"].iloc[-1] - df["time"].iloc[0]
                              ).total_seconds() / (86400 * 30.4))
        rows.append({
            "group": group,
            "label": label,
            "rr": r.rr,
            "n_trades": r.n_trades,
            "trades/mo": round(r.n_trades / span_mo, 2),
            "WR%": r.win_rate_pct,
            "PF": r.profit_factor,
            "kelly%": r.kelly_pct,
            "$/trade": round(r.net_pnl_dollars / r.n_trades, 2),
            "$/mo": round(r.net_pnl_dollars / span_mo, 1),
            "$_total": r.net_pnl_dollars,
            "max_DD%": r.max_dd_pct,
            "max_streak": r.max_consec_losses,
            "recov_d": r.recovery_days if r.recovery_days is not None else "—",
            "OOS_WR%": r.test_win_rate_pct,
            "score": r.survival_score,
        })

    if not rows:
        print("\nNo cells produced results. Check data/ parquets exist.")
        return 1

    df = pd.DataFrame(rows).sort_values("score", ascending=False)

    # ──────────── Terminal output (color-coded) ─────────────────────
    print("\n" + "═" * 130)
    print(BOLD(" TOP CELLS — UNIFIED COMPARISON  (cost-priced, ranked by survival score)"))
    print("═" * 130)
    print(f" {'#':>2}  {'group':<8}  {'cell':<32}  "
          f"{'R:R':>4}  {'N':>4}  {'tr/mo':>6}  {'WR%':>5}  "
          f"{'PF':>5}  {'kelly':>6}  {'$/tr':>7}  {'$/mo':>6}  "
          f"{'DD%':>6}  {'strk':>4}  {'recov':>5}  {'OOS%':>5}  "
          f"{'score':>6}  verdict")
    print("─" * 150)
    for i, (_, r) in enumerate(df.iterrows(), 1):
        recov = (f"{int(r['recov_d'])}d"
                   if isinstance(r["recov_d"], (int, float))
                   else r["recov_d"])
        print(f" {i:>2}  {r['group']:<8}  {r['label']:<32}  "
              f"{r['rr']:>4.1f}  {r['n_trades']:>4}  {r['trades/mo']:>6.2f}  "
              f"{_wr_color(r['WR%']):>14}  "
              f"{_pf_color(r['PF']):>14}  {r['kelly%']:>+5.1f}%  "
              f"{r['$/trade']:>+7.2f}  {r['$/mo']:>+6.1f}  "
              f"{_dd_color(r['max_DD%']):>16}  "
              f"{_streak_color(r['max_streak']):>13}  "
              f"{str(recov):>5}  {r['OOS_WR%']:>5.1f}  "
              f"{r['score']:>+6.1f}  {_verdict(r['PF'], r['score'])}")
    print()

    # ──────────── GOLD vs SILVER side-by-side ────────────────────────
    print("═" * 130)
    print(BOLD(" 🪙 METALS CORRELATION CHECK — gold vs silver vs palladium "))
    print("═" * 130)
    metals = df[df["group"].isin(["GOLD", "SILVER", "PALLAD."])].copy()
    if not metals.empty:
        metals["per_lot_$_per_year"] = (metals["$_total"]
                                              / (metals["n_trades"] / 12)
                                              * 12).round(0)
        cols = ["group", "label", "rr", "WR%", "PF",
                  "$/trade", "$/mo", "max_DD%", "max_streak", "score"]
        print(metals[cols].to_string(index=False))
    print()

    # ──────────── R:R sweet-spot for gold D1 (the unicorn cell) ──────
    print("═" * 130)
    print(BOLD(" 📊 R:R SWEET-SPOT — donchian_55 × XAUUSD × D1 across R:R values "))
    print("═" * 130)
    gold_d1 = df[df["label"].str.contains("XAUUSD D1")].sort_values("rr")
    if not gold_d1.empty:
        cols = ["rr", "WR%", "PF", "kelly%", "$/trade",
                  "$/mo", "max_DD%", "max_streak", "OOS_WR%", "score"]
        print(gold_d1[cols].to_string(index=False))
    print()

    # ──────────── CSV / HTML side outputs ────────────────────────────
    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"💾  CSV written → {args.csv}")
    if args.html:
        out = Path("/tmp/cells_compare.html")
        styled = (df.style
            .background_gradient(subset=["score", "PF", "kelly%", "$/mo"],
                                    cmap="RdYlGn")
            .background_gradient(subset=["max_DD%", "max_streak"],
                                    cmap="RdYlGn_r")
            .format({
                "PF": "{:.2f}", "WR%": "{:.1f}", "kelly%": "{:+.1f}",
                "$/trade": "${:+.2f}", "$/mo": "${:+.1f}",
                "$_total": "${:+.0f}", "max_DD%": "{:.2f}",
                "OOS_WR%": "{:.1f}", "score": "{:+.1f}",
            }))
        out.write_text(styled.to_html())
        print(f"🌐  HTML written → {out}")
        print(f"     Open with: open {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
