#!/usr/bin/env python3
"""sweep_improvements.py — apply 6 improvement layers to each top
deploy-safe cell and OOS-validate via production backtester.

Improvement layers:
  L1 baseline                       — original cell config
  L2 wider_target (RR 1:3)          — target_atr_mult = 4.5
  L3 tighter_stop (1.0× ATR)        — stop_atr_mult = 1.0
  L4 +200ema regime                 — only enter when close > 200-EMA
  L5 +rsi_filter                    — only enter when RSI(14) in [30, 60]
  L6 +prior_day_high_break          — only enter when bar high > prior day high
  L7 baseline_long_only             — drop SHORT signals
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path("/sessions/awesome-blissful-hopper/mnt/Documents/mt5_quant_trader_v2")
sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np
from core import cost_defaults
from core.backtest import run_backtest, partition_train_test, BacktestResult
from core.backtest_stats import compute_full_stats
from core.data import load_parquet
from core.edge_catalog import _evaluate_hard_gates
from core.indicators import ema, rsi_wilder
from core.symbol_info_loader import try_load
from core.strategy import Signal


# ---------- improvement filters ----------

def _filter_200ema(df, sigs):
    e = ema(df["close"], 200).values
    out = []
    for s in sigs:
        i = s.bar_idx
        if i >= len(e) or np.isnan(e[i]):
            continue
        if s.direction == "LONG" and df["close"].iloc[i] > e[i]:
            out.append(s)
        elif s.direction == "SHORT" and df["close"].iloc[i] < e[i]:
            out.append(s)
    return out


def _filter_rsi(df, sigs):
    r = rsi_wilder(df["close"], 14).values
    out = []
    for s in sigs:
        i = s.bar_idx
        if i >= len(r) or np.isnan(r[i]):
            continue
        if s.direction == "LONG" and 25 <= r[i] <= 60:
            out.append(s)
        elif s.direction == "SHORT" and 40 <= r[i] <= 75:
            out.append(s)
    return out


def _filter_prior_day_high(df, sigs):
    """Only enter LONG when current bar's high > previous day's high."""
    times = pd.to_datetime(df["time"])
    df2 = df.copy()
    df2["day"] = times.dt.normalize()
    daily_high = df2.groupby("day")["high"].max()
    daily_low = df2.groupby("day")["low"].min()
    day_arr = times.dt.normalize().values
    prior_day_high = {}
    prior_day_low = {}
    days = list(daily_high.index)
    for i, d in enumerate(days):
        if i > 0:
            prior_day_high[d] = float(daily_high.iloc[i - 1])
            prior_day_low[d] = float(daily_low.iloc[i - 1])
    out = []
    for s in sigs:
        i = s.bar_idx
        d = pd.Timestamp(day_arr[i])
        if s.direction == "LONG":
            pdh = prior_day_high.get(d)
            if pdh and df["high"].iloc[i] > pdh:
                out.append(s)
        elif s.direction == "SHORT":
            pdl = prior_day_low.get(d)
            if pdl and df["low"].iloc[i] < pdl:
                out.append(s)
    return out


def _drop_shorts(df, sigs):
    return [s for s in sigs if s.direction == "LONG"]


def _retarget(df, sigs, rr_mult):
    """Override target_price to be entry + rr_mult × stop_distance."""
    out = []
    for s in sigs:
        risk = abs(s.entry_price - s.stop_price)
        if s.direction == "LONG":
            new_target = s.entry_price + rr_mult * risk
        else:
            new_target = s.entry_price - rr_mult * risk
        out.append(Signal(
            bar_idx=s.bar_idx, direction=s.direction,
            entry_price=s.entry_price, stop_price=s.stop_price,
            target_price=new_target, reason=s.reason,
            max_hold_bars=s.max_hold_bars,
        ))
    return out


def _retighten_stop(df, sigs, atr_mult, atr_period=14):
    """Tighten stops to atr_mult × ATR from entry."""
    from core.indicators import atr_wilder
    a = atr_wilder(df, atr_period).values
    out = []
    for s in sigs:
        i = s.bar_idx
        if i >= len(a) or np.isnan(a[i]) or a[i] <= 0:
            continue
        if s.direction == "LONG":
            new_stop = s.entry_price - atr_mult * a[i]
            if new_stop >= s.entry_price:
                continue
            risk = s.entry_price - new_stop
            new_target = s.entry_price + 2.0 * risk    # keep R:R 1:2
        else:
            new_stop = s.entry_price + atr_mult * a[i]
            if new_stop <= s.entry_price:
                continue
            risk = new_stop - s.entry_price
            new_target = s.entry_price - 2.0 * risk
        out.append(Signal(
            bar_idx=s.bar_idx, direction=s.direction,
            entry_price=s.entry_price, stop_price=new_stop,
            target_price=new_target, reason=s.reason,
            max_hold_bars=s.max_hold_bars,
        ))
    return out


def _filter_volume_spike(df, sigs, mult=1.5, window=20):
    if "tick_volume" in df.columns:
        v = df["tick_volume"].values.astype(float)
    elif "volume" in df.columns:
        v = df["volume"].values.astype(float)
    else:
        v = (df["high"] - df["low"]).values.astype(float)
    avg = pd.Series(v).rolling(window).mean().values
    out = []
    for s in sigs:
        i = s.bar_idx
        if i >= len(avg) or np.isnan(avg[i]) or avg[i] <= 0:
            continue
        if v[i] > mult * avg[i]:
            out.append(s)
    return out


def _filter_macd_momentum(df, sigs):
    """Require MACD histogram to align + be rising (LONG) / falling (SHORT)."""
    closes = df["close"]
    ema12 = ema(closes, 12).values
    ema26 = ema(closes, 26).values
    macd = ema12 - ema26
    signal_line = ema(pd.Series(macd), 9).values
    hist = macd - signal_line
    out = []
    for s in sigs:
        i = s.bar_idx
        if i < 35 or np.isnan(hist[i]) or np.isnan(hist[i - 1]):
            continue
        if s.direction == "LONG":
            if macd[i] > signal_line[i] and hist[i] > hist[i - 1]:
                out.append(s)
        else:
            if macd[i] < signal_line[i] and hist[i] < hist[i - 1]:
                out.append(s)
    return out


def _filter_atr_vol_regime(df, sigs, lookback=50, min_pctile=0.40):
    """Only enter when ATR is in upper 60% of its rolling range. Skips
    dead-vol regimes where stops get noise-tagged."""
    from core.indicators import atr_wilder
    a = atr_wilder(df, 14).values
    a_s = pd.Series(a)
    rmin = a_s.rolling(lookback).min().values
    rmax = a_s.rolling(lookback).max().values
    out = []
    for s in sigs:
        i = s.bar_idx
        if (i >= len(a) or np.isnan(rmax[i])
                or rmax[i] <= rmin[i]):
            continue
        pctile = (a[i] - rmin[i]) / (rmax[i] - rmin[i])
        if pctile >= min_pctile:
            out.append(s)
    return out


# ---------- cells under test ----------

def _ema_cross(fast, slow, stop=1.5, target=3.0, long_only=False):
    from strategies.ema_cross import EmaCross, EmaCrossParams
    return EmaCross(EmaCrossParams(fast_period=fast, slow_period=slow,
                                     atr_period=14,
                                     stop_atr_mult=stop, target_atr_mult=target,
                                     long_only=long_only))


def _donchian(period, stop=1.5, target=3.0, long_only=False):
    from strategies.donchian_breakout import (
        DonchianBreakout, DonchianBreakoutParams,
    )
    return DonchianBreakout(DonchianBreakoutParams(
        period=period, atr_period=14,
        stop_atr_mult=stop, target_atr_mult=target,
        long_only=long_only))


def _rsi(stop=1.5, target=2.0, long_only=False, ob=70, os=30):
    from strategies.rsi_meanrev import RsiMeanRev, RsiMeanRevParams
    return RsiMeanRev(RsiMeanRevParams(
        rsi_period=14, oversold=os, overbought=ob, atr_period=14,
        stop_atr_mult=stop, target_atr_mult=target,
        long_only=long_only))


CELLS = [
    # (label, ticker, tf, mpu, strategy_factory)
    ("rsi EURUSD M15",   "EURUSD",     "M15", 1.0,    lambda: _rsi(long_only=True)),
    ("rsi XPDUSD H1",    "XPDUSD",     "H1",  100.0,  lambda: _rsi(long_only=True)),
    ("donch55 XAUUSD H1","XAUUSD",     "H1",  100.0,  lambda: _donchian(55, target=4.5, long_only=True)),
    ("ema9_20 US100 M15","US100.cash", "M15", 1.0,    lambda: _ema_cross(9, 20)),
    ("ema12_26 HK50 M15","HK50.cash",  "M15", 1.0,    lambda: _ema_cross(12, 26, target=3.0, long_only=True)),
    ("donch55 US100 D1", "US100.cash", "D1",  1.0,    lambda: _donchian(55)),
    ("donch20 US100 M15","US100.cash", "M15", 1.0,    lambda: _donchian(20)),
    ("ema12_26 US100 H1","US100.cash", "H1",  1.0,    lambda: _ema_cross(12, 26)),
    ("ema9_20 JP225 M15","JP225.cash", "M15", 1.0,    lambda: _ema_cross(9, 20)),
    ("ema12_26 HK50 H1", "HK50.cash",  "H1",  1.0,    lambda: _ema_cross(12, 26, target=3.0, long_only=True)),
    ("donch20 JP225 M15","JP225.cash", "M15", 1.0,    lambda: _donchian(20)),
    ("ema12_26 US30 H1", "US30.cash",  "H1",  3.0,    lambda: _ema_cross(12, 26)),
]

LAYERS = [
    ("L1_baseline",            None),
    ("L2_wider_target_RR1:3",  ("retarget", 3.0)),
    ("L3_tighter_stop_1.0atr", ("tighten_stop", 1.0)),
    ("L4_+200ema",             ("filter_200ema", None)),
    ("L5_+rsi_filter",         ("filter_rsi", None)),
    ("L6_+prior_day_break",    ("filter_pdh", None)),
    ("L7_long_only",           ("drop_shorts", None)),
    ("L8_+volume_spike",       ("filter_volume", None)),
    ("L9_+macd_momentum",      ("filter_macd", None)),
    ("L10_+atr_vol_regime",    ("filter_atr_vol", None)),
    ("L11_200ema+long_only",   ("combo_200ema_long", None)),
    ("L12_200ema+RR1:3",       ("combo_200ema_rr3", None)),
]


def _apply_layer(df, sigs, layer_action):
    if layer_action is None:
        return sigs
    op, arg = layer_action
    if op == "retarget":
        return _retarget(df, sigs, arg)
    if op == "tighten_stop":
        return _retighten_stop(df, sigs, arg)
    if op == "filter_200ema":
        return _filter_200ema(df, sigs)
    if op == "filter_rsi":
        return _filter_rsi(df, sigs)
    if op == "filter_pdh":
        return _filter_prior_day_high(df, sigs)
    if op == "drop_shorts":
        return _drop_shorts(df, sigs)
    if op == "filter_volume":
        return _filter_volume_spike(df, sigs)
    if op == "filter_macd":
        return _filter_macd_momentum(df, sigs)
    if op == "filter_atr_vol":
        return _filter_atr_vol_regime(df, sigs)
    if op == "combo_200ema_long":
        return _filter_200ema(df, _drop_shorts(df, sigs))
    if op == "combo_200ema_rr3":
        return _filter_200ema(df, _retarget(df, sigs, 3.0))
    return sigs


def _evaluate(df, sigs_filtered, mpu, ticker, sname):
    sym = try_load(ticker)
    if not sigs_filtered:
        return None
    res = run_backtest(
        df, sigs_filtered, starting_balance=100_000.0, lots=0.0,
        money_per_unit_price=mpu,
        commission_per_trade=cost_defaults.DEFAULT_COMMISSION_USD,
        slippage_per_fill_atr_frac=cost_defaults.DEFAULT_SLIPPAGE_ATR_FRAC,
        risk_pct=0.30, symbol_info=sym, symbol=ticker,
    )
    train, test = partition_train_test(res, train_pct=0.6, n_bars=len(df))
    split_idx = int(len(df) * 0.6)
    test_trades = [t for t in res.trades if t.entry_bar_idx >= split_idx]
    if test_trades:
        synth = BacktestResult(
            trades=test_trades, equity_curve=res.equity_curve,
            starting_balance=100_000.0,
            ending_balance=100_000.0 + sum(t.realized_pnl for t in test_trades),
            sum_realized_pnl=sum(t.realized_pnl for t in test_trades),
            equity_curve_pnl=sum(t.realized_pnl for t in test_trades),
            reconciles=True,
        )
        oos = compute_full_stats(synth, starting_balance=100_000.0)
        rec = oos.recovery_duration_days
        oos_dd = oos.max_dd_pct
    else:
        rec = None
        oos_dd = 0.0
    pf_te = 99.99 if test.profit_factor == float("inf") else test.profit_factor
    pf_tr = 99.99 if train.profit_factor == float("inf") else train.profit_factor
    gates = _evaluate_hard_gates(
        overall_pf=pf_te, test_pf=pf_te, test_r=test.avg_R,
        recovery_days=rec, n_test=test.n_trades,
        strategy_name=sname,
    )
    return dict(
        n_train=train.n_trades, n_test=test.n_trades,
        pf_train=pf_tr, pf_test=pf_te,
        r_test=test.avg_R, wr_test=test.win_rate,
        oos_dd_pct=oos_dd, recov_oos=rec,
        net_pnl=res.sum_realized_pnl,
        deploy_safe=(len(gates) == 0),
        gates_failed=gates,
    )


def main():
    print(f"{'Cell':22} {'Layer':24} {'n_te':>5} {'PF_te':>6} "
          f"{'R_te':>6} {'DD%':>5} {'recov':>7} {'net$':>10} {'verdict':>8}")
    print("-" * 110)
    promotes = []
    for label, ticker, tf, mpu, factory in CELLS:
        try:
            df = load_parquet(ROOT / "data" / f"{ticker}_{tf}.parquet")
        except Exception as e:
            print(f"{label:22} skip: {e}")
            continue
        # Get baseline signals once
        try:
            base_sigs = factory().signals(df)
        except Exception as e:
            print(f"{label:22} build err: {e}")
            continue
        if not base_sigs:
            print(f"{label:22} no baseline signals")
            continue

        baseline_metrics = None
        for layer_name, layer_action in LAYERS:
            sigs = _apply_layer(df, base_sigs, layer_action)
            sname_raw = label.split()[0]
            try:
                m = _evaluate(df, sigs, mpu, ticker, sname_raw)
            except Exception as e:
                print(f"{label:22} {layer_name:24} ERROR: {e!r}"[:100])
                continue
            if m is None:
                print(f"{label:22} {layer_name:24} (0 sigs after filter)")
                continue
            if layer_name == "L1_baseline":
                baseline_metrics = m
            verdict = "✅" if m["deploy_safe"] else "🚫"
            improved = ""
            if (baseline_metrics and layer_name != "L1_baseline"
                    and m["deploy_safe"]
                    and (m["pf_test"] > baseline_metrics["pf_test"] * 1.05
                         or m["r_test"] > baseline_metrics["r_test"] + 0.05)):
                improved = "🚀"
            rec_s = (f"{m['recov_oos']:.0f}d"
                      if m["recov_oos"] is not None else "?")
            print(f"{label[:22]:22} {layer_name:24} {m['n_test']:>5} "
                  f"{m['pf_test']:>6.2f} {m['r_test']:>+6.2f} "
                  f"{m['oos_dd_pct']:>4.1f}% {rec_s:>7} "
                  f"${m['net_pnl']:>+8,.0f} {verdict}{improved:>2}")
            if improved:
                promotes.append((label, layer_name, m, baseline_metrics))
        print()

    print("=" * 110)
    print(f"IMPROVEMENT WINNERS — {len(promotes)} cells beat baseline")
    print("=" * 110)
    for label, layer, m, base in promotes:
        print(f"  {label:22} via {layer:24}")
        print(f"     PF: {base['pf_test']:.2f} → {m['pf_test']:.2f}  "
              f"R: {base['r_test']:+.2f} → {m['r_test']:+.2f}  "
              f"recov: {(str(int(base['recov_oos']))+'d') if base['recov_oos'] else '?'} → "
              f"{(str(int(m['recov_oos']))+'d') if m['recov_oos'] else '?'}")


if __name__ == "__main__":
    main()
