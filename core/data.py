"""
data.py — load + validate broker candles, persist to parquet.

Three concerns separated cleanly:
  1. fetch_from_bridge(symbol, tf, n_bars) — talks to MT5 file bridge
  2. load_parquet(path)                    — read locked test fixtures
  3. ensure_clean(df)                      — validate invariants, raise loudly

The whole pipeline is idempotent: run twice with same params → identical bytes.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.storage import validate_candles


def load_parquet(path: str | Path) -> pd.DataFrame:
    """Read a locked-data parquet file and validate it before returning."""
    df = pd.read_parquet(path)
    # Normalize: time column must be a tz-aware UTC pd.Timestamp
    if not pd.api.types.is_datetime64_any_dtype(df["time"]):
        df["time"] = pd.to_datetime(df["time"], utc=True)
    if df["time"].dt.tz is None:
        df["time"] = df["time"].dt.tz_localize("UTC")
    elif df["time"].dt.tz != timezone.utc:
        df["time"] = df["time"].dt.tz_convert("UTC")
    validate_candles(df)
    return df


def save_parquet(df: pd.DataFrame, path: str | Path) -> Path:
    """Write a validated candle DataFrame to parquet. Idempotent for same df."""
    validate_candles(df)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure consistent column order so the same df produces same bytes
    df = df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    df.to_parquet(path, index=False, compression="snappy")
    return path


# ---------------------------------------------------------------------------
# MT5 file-bridge fetch
# ---------------------------------------------------------------------------
# Matches v1's working file-bridge layout (mt5qt/req + mt5qt/rep under
# the Wine MetaTrader 5 sandbox).
DEFAULT_BRIDGE_DIR = (
    Path.home()
    / "Library/Application Support/net.metaquotes.wine.metatrader5"
    / "drive_c/Program Files/MetaTrader 5/MQL5/Files/mt5qt"
)


def fetch_from_bridge(symbol: str, tf: str, n_bars: int,
                      bridge_dir: str | Path = DEFAULT_BRIDGE_DIR,
                      timeout_s: float = 30.0) -> pd.DataFrame:
    """Request candles via the MT5 file bridge.

    The MT5 EA polls `req/` for JSON requests and writes responses to `rep/`.
    Protocol: write {id, method:"candles", params:{symbol, tf, n}} → poll for response.

    Returns a validated, tz-aware DataFrame.
    """
    bridge_dir = Path(bridge_dir)
    req_dir = bridge_dir / "req"
    rep_dir = bridge_dir / "rep"
    req_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)

    # Method name + param keys must match v1's working bridge protocol exactly.
    # See ~/Documents/mt5_quant_trader/src/mt5_client.py:329 — the EA only
    # responds to "copy_rates" with {"name", "timeframe", "count"}.
    rid = uuid.uuid4().hex
    req = {"id": rid, "method": "copy_rates",
           "params": {"name": symbol, "timeframe": tf, "count": int(n_bars)}}
    req_path = req_dir / f"{rid}.json"
    rep_path = rep_dir / f"{rid}.json"

    # Atomic write
    tmp = req_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(req, separators=(",", ":")), encoding="utf-8")
    tmp.rename(req_path)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if rep_path.exists():
            try:
                size = rep_path.stat().st_size
                if size == 0:
                    time.sleep(0.05); continue
                body = rep_path.read_text(encoding="utf-8", errors="replace").strip()
                if not body:
                    time.sleep(0.05); continue
                resp = json.loads(body)
                rep_path.unlink(missing_ok=True)
                req_path.unlink(missing_ok=True)
                break
            except (json.JSONDecodeError, FileNotFoundError):
                time.sleep(0.05); continue
        time.sleep(0.05)
    else:
        raise TimeoutError(f"bridge did not respond within {timeout_s}s")

    # v1's bridge returns a list of bar-dicts directly (or wraps under a key).
    # Accept either shape so we're robust to EA versions.
    if isinstance(resp, list):
        rows = resp
    elif isinstance(resp, dict):
        # Most likely: {"id": "...", "ok": true, "data": [...]}
        # or a top-level list of bars under some key
        rows = (resp.get("data") or resp.get("rates")
                or resp.get("candles") or resp.get("bars"))
        if rows is None:
            # Some bridges return the bars at the top level WITH metadata
            # alongside — strip out non-bar keys
            non_bar_keys = {"id", "ok", "error", "method"}
            possible = {k: v for k, v in resp.items() if k not in non_bar_keys}
            # If exactly one remaining key is a list, use it
            list_vals = [v for v in possible.values() if isinstance(v, list)]
            if len(list_vals) == 1:
                rows = list_vals[0]
        if rows is None:
            raise RuntimeError(
                f"bridge response had no recognisable bars list: keys={list(resp.keys())}"
            )
    else:
        raise RuntimeError(f"unexpected bridge response type: {type(resp)}")

    if not rows:
        raise RuntimeError(f"bridge returned 0 candles for {symbol} {tf}")

    df = pd.DataFrame(rows)
    # The bridge may name columns "time"/"o"/"h"/"l"/"c"/"v" — normalize
    rename = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
              "tick_volume": "volume"}
    df = df.rename(columns=rename)

    # Times: bridge typically returns epoch seconds. Convert to tz-aware UTC.
    if "time" in df.columns:
        if pd.api.types.is_numeric_dtype(df["time"]):
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        else:
            df["time"] = pd.to_datetime(df["time"], utc=True)
    else:
        raise RuntimeError("bridge response missing 'time' column")

    # Sort + dedup just in case
    df = df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
    df = df[["time", "open", "high", "low", "close", "volume"]]

    validate_candles(df)
    return df
