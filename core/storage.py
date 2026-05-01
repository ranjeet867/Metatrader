"""
storage.py — SQLite schema + idempotent persistence.

Design principles:
  - WAL mode for durability + concurrent reads
  - All writes are idempotent: re-inserting the same trade does not corrupt state
  - Natural keys (no implicit autoinc as primary key for trades — use deterministic key)
  - Schema migrations are versioned and idempotent

Tables:
  - candles (symbol, tf, time, ohlcv) — historical price data
  - trades  (id, opened_at, ...)      — backtest/paper/live trade records
  - runs    (run_id, started_at, ...) — backtest run metadata + reconciliation status
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_DDL = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candles (
        symbol      TEXT    NOT NULL,
        tf          TEXT    NOT NULL,
        time_utc    TEXT    NOT NULL,        -- ISO8601 UTC
        open        REAL    NOT NULL CHECK (open  > 0),
        high        REAL    NOT NULL CHECK (high  > 0),
        low         REAL    NOT NULL CHECK (low   > 0),
        close       REAL    NOT NULL CHECK (close > 0),
        volume      REAL    NOT NULL CHECK (volume >= 0),
        PRIMARY KEY (symbol, tf, time_utc),
        CHECK (high >= low),
        CHECK (high >= open),
        CHECK (high >= close),
        CHECK (low <= open),
        CHECK (low <= close)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_candles_time
        ON candles (symbol, tf, time_utc)
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id              TEXT PRIMARY KEY,
        started_at_utc      TEXT NOT NULL,
        finished_at_utc     TEXT,
        symbol              TEXT NOT NULL,
        tf                  TEXT NOT NULL,
        strategy_name       TEXT NOT NULL,
        config_json         TEXT NOT NULL,
        starting_balance    REAL NOT NULL,
        ending_equity       REAL,
        n_trades            INTEGER,
        sum_realized_pnl    REAL,
        equity_curve_pnl    REAL,
        reconciles          INTEGER         -- 1 if abs(sum_pnl - eq_pnl) < 0.01
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        run_id          TEXT NOT NULL,
        trade_idx       INTEGER NOT NULL,           -- 0-based index within the run
        symbol          TEXT NOT NULL,
        direction       TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
        opened_at_utc   TEXT NOT NULL,
        closed_at_utc   TEXT,
        entry_price     REAL NOT NULL CHECK (entry_price > 0),
        stop_price      REAL NOT NULL CHECK (stop_price > 0),
        target_price    REAL CHECK (target_price IS NULL OR target_price > 0),
        exit_price      REAL CHECK (exit_price IS NULL OR exit_price > 0),
        lots            REAL NOT NULL CHECK (lots > 0),
        realized_pnl    REAL,
        r_multiple      REAL,
        close_reason    TEXT,
        PRIMARY KEY (run_id, trade_idx),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
]


@contextmanager
def connect(db_path: str | Path):
    """Context manager that yields a sqlite3.Connection with WAL + foreign keys."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None)  # autocommit
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        yield conn
    finally:
        conn.close()


def init_schema(db_path: str | Path) -> None:
    """Create tables if they don't exist. Safe to call multiple times."""
    with connect(db_path) as c:
        for ddl in SCHEMA_DDL:
            c.execute(ddl)
        # Record version (idempotent — INSERT OR IGNORE)
        c.execute(
            "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )


def validate_candles(df) -> None:
    """Raise ValueError if any candle row violates OHLC invariants.

    We validate BEFORE persisting so bad data fails loudly. Without this,
    INSERT OR IGNORE silently drops rows that violate CHECK constraints,
    masking data quality issues.
    """
    required = {"time", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"candles missing columns: {missing}")
    if df.empty:
        return
    # Vectorized invariant checks
    if (df["open"] <= 0).any():
        bad = df[df["open"] <= 0].head(1)
        raise ValueError(f"open <= 0: {bad.to_dict('records')}")
    if (df["high"] <= 0).any():
        bad = df[df["high"] <= 0].head(1)
        raise ValueError(f"high <= 0: {bad.to_dict('records')}")
    if (df["low"] <= 0).any():
        bad = df[df["low"] <= 0].head(1)
        raise ValueError(f"low <= 0: {bad.to_dict('records')}")
    if (df["close"] <= 0).any():
        bad = df[df["close"] <= 0].head(1)
        raise ValueError(f"close <= 0: {bad.to_dict('records')}")
    if (df["volume"] < 0).any():
        raise ValueError("volume < 0 detected")
    if (df["high"] < df["low"]).any():
        bad = df[df["high"] < df["low"]].head(1)
        raise ValueError(f"high < low: {bad.to_dict('records')}")
    if (df["high"] < df["open"]).any() or (df["high"] < df["close"]).any():
        raise ValueError("high < open or high < close")
    if (df["low"] > df["open"]).any() or (df["low"] > df["close"]).any():
        raise ValueError("low > open or low > close")
    # Time must be monotonically increasing and unique
    if not df["time"].is_monotonic_increasing:
        raise ValueError("time column not monotonically increasing")
    if df["time"].duplicated().any():
        raise ValueError("duplicate timestamps detected")


def save_candles(db_path: str | Path, symbol: str, tf: str, df) -> int:
    """Persist candle rows. Validates invariants BEFORE insert.
    Idempotent on (symbol, tf, time) via INSERT OR IGNORE."""
    validate_candles(df)
    rows = [
        (symbol, tf, t.isoformat(), float(o), float(h), float(l), float(c), float(v))
        for t, o, h, l, c, v in zip(
            df["time"], df["open"], df["high"], df["low"], df["close"], df["volume"]
        )
    ]
    with connect(db_path) as c:
        cur = c.executemany(
            """
            INSERT OR IGNORE INTO candles
                (symbol, tf, time_utc, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return cur.rowcount


def load_candles(db_path: str | Path, symbol: str, tf: str):
    """Return all candles for (symbol, tf) sorted by time, as a DataFrame."""
    import pandas as pd
    with connect(db_path) as c:
        rows = c.execute(
            "SELECT time_utc, open, high, low, close, volume FROM candles "
            "WHERE symbol = ? AND tf = ? ORDER BY time_utc ASC",
            (symbol, tf),
        ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df


def save_run(db_path: str | Path, run_id: str, *, started_at_utc: str, symbol: str,
              tf: str, strategy_name: str, config_json: str,
              starting_balance: float) -> None:
    """Insert a new run row. Fails if run_id already exists (intentional)."""
    with connect(db_path) as c:
        c.execute(
            """
            INSERT INTO runs (run_id, started_at_utc, symbol, tf, strategy_name,
                              config_json, starting_balance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, started_at_utc, symbol, tf, strategy_name, config_json,
             starting_balance),
        )


def finish_run(db_path: str | Path, run_id: str, *, finished_at_utc: str,
                ending_equity: float, n_trades: int,
                sum_realized_pnl: float, equity_curve_pnl: float,
                reconciles: bool) -> None:
    """Update run row with final reconciliation results."""
    with connect(db_path) as c:
        c.execute(
            """
            UPDATE runs
               SET finished_at_utc = ?,
                   ending_equity = ?,
                   n_trades = ?,
                   sum_realized_pnl = ?,
                   equity_curve_pnl = ?,
                   reconciles = ?
             WHERE run_id = ?
            """,
            (finished_at_utc, ending_equity, n_trades, sum_realized_pnl,
             equity_curve_pnl, 1 if reconciles else 0, run_id),
        )


def save_trades(db_path: str | Path, run_id: str, trades: list[dict]) -> int:
    """Persist trade rows. Idempotent via PRIMARY KEY (run_id, trade_idx)."""
    rows = [
        (run_id, i, t["symbol"], t["direction"],
         t["opened_at_utc"], t.get("closed_at_utc"),
         float(t["entry_price"]), float(t["stop_price"]),
         t.get("target_price"), t.get("exit_price"),
         float(t["lots"]),
         t.get("realized_pnl"), t.get("r_multiple"),
         t.get("close_reason"))
        for i, t in enumerate(trades)
    ]
    with connect(db_path) as c:
        cur = c.executemany(
            """
            INSERT OR REPLACE INTO trades
              (run_id, trade_idx, symbol, direction, opened_at_utc, closed_at_utc,
               entry_price, stop_price, target_price, exit_price,
               lots, realized_pnl, r_multiple, close_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return cur.rowcount
