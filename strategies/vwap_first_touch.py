"""
vwap_first_touch.py — disciplined intraday VWAP bounce on H1.

The naive H1 VWAP bounce fired 500-800 trades per cell — too noisy.
This version is far more selective:

  Setup (per UTC day):
    1. Wait for VWAP to settle (min_bars_after_anchor bars after 00:00)
    2. Track if price was above VWAP since anchor settled (bullish day)
    3. FIRST time bar low touches VWAP after that:
        - close > open  (bounce candle)
        - RSI(14) on H1 < 50  (pullback, not breakout-chase)
        - 50-EMA > 200-EMA on D1  (regime filter — supplied by runner
          via candles already on H1 — we approximate with 200-EMA on H1)
    Only ONE trade per UTC day.
    Stop = VWAP - stop_atr_pad × ATR

  Exit: fixed R:R (1:1, 1:2, 1:3 supported here). The "trail until
  close < VWAP" exit (PF 1.77 on US100 H1) needs trailing-exit support
  in the backtester — see scripts/sweep_vwap_first_touch.py.

Empirically best fixed-R:R cells (optimization_2026-05-06):
  - US100.cash H1 R:R 1:2 → PF 1.38 over 101 trades, 5.0% DD
  - XPTUSD H1   R:R 1:2 → PF 1.58 over 46 trades, 2.5% DD
  - FRA40.cash H1 R:R 1:2 → PF 1.36, 1.3% DD
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.indicators import ema, atr_wilder, rsi_wilder
from core.strategy import Signal


@dataclass(frozen=True)
class VwapFirstTouchParams:
    touch_pct: float = 0.002           # within 0.2% of VWAP
    stop_atr_pad: float = 0.7
    min_bars_after_anchor: int = 5
    rsi_max_at_entry: float = 50.0
    rsi_period: int = 14
    atr_period: int = 14
    rr_target: float = 2.0             # 1:1, 1:2, 1:3
    use_regime_filter: bool = True     # 50-EMA > 200-EMA on the H1 series


def _session_vwap(df: pd.DataFrame):
    """Anchor VWAP at each UTC day's start; return vwap, bars_in_session,
    and is_new_day flag (booleans aligned to df rows)."""
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"])
    if "tick_volume" in df.columns:
        vol = df["tick_volume"].values.astype(float)
    elif "volume" in df.columns:
        vol = df["volume"].values.astype(float)
    else:
        vol = (df["high"] - df["low"]).values.astype(float)
    vol = np.where(vol > 0, vol, 1.0)
    tp = ((df["high"] + df["low"] + df["close"]) / 3.0).values
    day_id = df["time"].dt.normalize().values

    vwap = np.empty(len(df))
    bars_in_sess = np.zeros(len(df), dtype=int)
    is_new_day = np.zeros(len(df), dtype=bool)
    cum_pv = 0.0
    cum_v = 0.0
    cur_day = None
    bar_in_session = 0
    for i in range(len(df)):
        if cur_day != day_id[i]:
            cum_pv = 0.0
            cum_v = 0.0
            cur_day = day_id[i]
            bar_in_session = 0
            is_new_day[i] = True
        cum_pv += tp[i] * vol[i]
        cum_v += vol[i]
        bar_in_session += 1
        bars_in_sess[i] = bar_in_session
        vwap[i] = cum_pv / cum_v if cum_v > 0 else tp[i]
    return vwap, bars_in_sess, is_new_day


class VwapFirstTouch:
    name = "vwap_first_touch"

    def __init__(self, params: VwapFirstTouchParams
                 = VwapFirstTouchParams()):
        self.params = params

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        p = self.params
        n = len(candles)
        if n < 250:
            return []
        vwap, bars_in_sess, is_new_day = _session_vwap(candles)
        atr = atr_wilder(candles, p.atr_period).values
        rsi = rsi_wilder(candles["close"], p.rsi_period).values
        c = candles["close"].values
        o = candles["open"].values
        h = candles["high"].values
        l = candles["low"].values

        if p.use_regime_filter:
            e50 = ema(candles["close"], 50).values
            e200 = ema(candles["close"], 200).values
        else:
            e50 = e200 = None

        out: list[Signal] = []
        today_was_above_vwap = False
        today_traded = False
        for i in range(60, n):
            if is_new_day[i]:
                today_was_above_vwap = False
                today_traded = False
            if today_traded:
                if c[i] > vwap[i]:
                    today_was_above_vwap = True
                continue
            if bars_in_sess[i] < p.min_bars_after_anchor:
                if c[i] > vwap[i]:
                    today_was_above_vwap = True
                continue
            if not today_was_above_vwap:
                if c[i] > vwap[i]:
                    today_was_above_vwap = True
                continue
            if np.isnan(vwap[i]) or atr[i] <= 0 or np.isnan(rsi[i]):
                continue
            if p.use_regime_filter and not (e50[i] > e200[i]):
                continue
            touched = (l[i] <= vwap[i] * (1 + p.touch_pct)
                       and l[i] >= vwap[i] * (1 - p.touch_pct))
            bounced = c[i] > o[i] and c[i] > vwap[i]
            if not (touched and bounced and rsi[i] < p.rsi_max_at_entry):
                if c[i] > vwap[i]:
                    today_was_above_vwap = True
                continue
            stop = vwap[i] - p.stop_atr_pad * atr[i]
            if stop <= 0 or stop >= c[i]:
                continue
            risk = c[i] - stop
            target = c[i] + p.rr_target * risk
            out.append(Signal(
                bar_idx=i, direction="LONG",
                entry_price=float(c[i]),
                stop_price=float(stop),
                target_price=float(target),
                reason="vwap_first_touch",
            ))
            today_traded = True
            if c[i] > vwap[i]:
                today_was_above_vwap = True
        return out
