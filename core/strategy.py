"""
strategy.py — base class + Signal dataclass.

A Strategy is a PURE function: given a DataFrame of candles + indicators,
return a list of Signal objects. No state, no I/O, no global config.

The backtester is responsible for execution, sizing, and management.
The strategy ONLY decides "should we enter LONG/SHORT at this bar?"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import pandas as pd


Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class Signal:
    """A trade entry signal. Emitted by a strategy on a specific bar."""
    bar_idx: int                    # 0-based index into the candle DataFrame
    direction: Direction
    entry_price: float              # the close of the signal bar (what we'll fill at)
    stop_price: float               # initial stop-loss
    target_price: float             # initial take-profit
    reason: str = ""                # human-readable for debugging


class Strategy(Protocol):
    """Any callable that takes a candle DataFrame and returns Signals."""
    name: str

    def signals(self, candles: pd.DataFrame) -> list[Signal]:
        ...
