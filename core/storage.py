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


SCHEMA_VERSION = 2

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
        -- v2 metadata (added inline so a fresh DB has them; the migration
        -- helper handles v1 DBs that still have the older shape).
        mode            TEXT NOT NULL DEFAULT 'backtest'
                                CHECK (mode IN ('backtest','paper','live')),
        strategy        TEXT,
        tf              TEXT,
        idempotency_key TEXT,
        notes           TEXT,
        mt5_ticket      INTEGER,
        magic_number    INTEGER,
        PRIMARY KEY (run_id, trade_idx),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
    # ----- v2 additions -----
    """
    CREATE TABLE IF NOT EXISTS paper_runs (
        run_id              TEXT PRIMARY KEY,
        started_at_utc      TEXT NOT NULL,
        finished_at_utc     TEXT,
        status              TEXT CHECK (status IN ('running','stopped','crashed','finished')),
        config_json         TEXT NOT NULL,
        heartbeat_at_utc    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_runs (
        run_id              TEXT PRIMARY KEY,
        started_at_utc      TEXT NOT NULL,
        finished_at_utc     TEXT,
        status              TEXT CHECK (status IN ('running','stopped','crashed','finished')),
        config_json         TEXT NOT NULL,
        heartbeat_at_utc    TEXT,
        account_login       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_state (
        symbol              TEXT NOT NULL,
        strategy            TEXT NOT NULL,
        consecutive_losses  INTEGER DEFAULT 0,
        last_loss_at_utc    TEXT,
        cooldown_until_utc  TEXT,
        daily_loss_pct      REAL DEFAULT 0,
        day_start_balance   REAL,
        PRIMARY KEY (symbol, strategy)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ftmo_daily_resets (
        reset_at_utc        TEXT PRIMARY KEY,
        day_start_balance   REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS parity_log (
        strategy            TEXT NOT NULL,
        passed_at_utc       TEXT NOT NULL,
        divergence_dollars  REAL NOT NULL,
        PRIMARY KEY (strategy, passed_at_utc)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bridge_events (
        pinged_at_utc       TEXT,
        method              TEXT,
        ok                  INTEGER,
        latency_ms          INTEGER,
        error               TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_bridge_events_time
        ON bridge_events (pinged_at_utc)
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_notes (
        trade_run_id        TEXT NOT NULL,
        trade_idx           INTEGER NOT NULL,
        note                TEXT,
        updated_at_utc      TEXT,
        PRIMARY KEY (trade_run_id, trade_idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forced_flat_events (
        occurred_at_utc     TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        mode                TEXT NOT NULL CHECK (mode IN ('backtest','paper','live')),
        reason              TEXT NOT NULL CHECK (reason IN ('weekend_flat','daily_close_flat')),
        mark_price          REAL,
        pnl_at_close        REAL,
        PRIMARY KEY (occurred_at_utc, symbol)
    )
    """,
]


# Idempotent column additions on existing trades table.
# Each entry: (column_name, DDL fragment after ADD COLUMN).
TRADES_NEW_COLUMNS: list[tuple[str, str]] = [
    ("mode",            "TEXT NOT NULL DEFAULT 'backtest'"),
    ("strategy",        "TEXT"),
    ("tf",              "TEXT"),
    ("idempotency_key", "TEXT"),
    ("notes",           "TEXT"),
    ("mt5_ticket",      "INTEGER"),
    ("magic_number",    "INTEGER"),
]


def _existing_columns(conn, table: str) -> set[str]:
    """Return set of column names currently on `table`. Empty if table missing."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _migrate_trades(conn) -> None:
    """Add columns to trades that are missing. Idempotent — never re-adds."""
    have = _existing_columns(conn, "trades")
    if not have:
        # Table doesn't exist yet — CREATE TABLE will run from SCHEMA_DDL.
        return
    for col, ddl in TRADES_NEW_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {ddl}")


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
    """Create tables + apply column migrations. Safe to call multiple times."""
    with connect(db_path) as c:
        for ddl in SCHEMA_DDL:
            c.execute(ddl)
        # If we landed on a pre-v2 DB whose `trades` was created without the
        # new columns, ALTER them in. (CREATE TABLE IF NOT EXISTS is a no-op on
        # an existing v1 table — that's why we need this second pass.)
        _migrate_trades(c)
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
    """Persist trade rows. Idempotent via PRIMARY KEY (run_id, trade_idx).

    The trade dicts may include the v2 metadata fields (mode, strategy, tf,
    idempotency_key, notes, mt5_ticket, magic_number); all default sensibly.
    """
    rows = [
        (run_id, i, t["symbol"], t["direction"],
         t["opened_at_utc"], t.get("closed_at_utc"),
         float(t["entry_price"]), float(t["stop_price"]),
         t.get("target_price"), t.get("exit_price"),
         float(t["lots"]),
         t.get("realized_pnl"), t.get("r_multiple"),
         t.get("close_reason"),
         t.get("mode", "backtest"), t.get("strategy"), t.get("tf"),
         t.get("idempotency_key"), t.get("notes"),
         t.get("mt5_ticket"), t.get("magic_number"))
        for i, t in enumerate(trades)
    ]
    with connect(db_path) as c:
        cur = c.executemany(
            """
            INSERT OR REPLACE INTO trades
              (run_id, trade_idx, symbol, direction, opened_at_utc, closed_at_utc,
               entry_price, stop_price, target_price, exit_price,
               lots, realized_pnl, r_multiple, close_reason,
               mode, strategy, tf, idempotency_key, notes, mt5_ticket, magic_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# v2 paper / live / risk / parity / bridge / notes writers
# ---------------------------------------------------------------------------

def upsert_paper_run(db_path: str | Path, run_id: str, *, started_at_utc: str,
                     status: str, config_json: str,
                     finished_at_utc: str | None = None,
                     heartbeat_at_utc: str | None = None) -> None:
    with connect(db_path) as c:
        c.execute(
            """
            INSERT INTO paper_runs (run_id, started_at_utc, finished_at_utc,
                                     status, config_json, heartbeat_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                finished_at_utc = excluded.finished_at_utc,
                status = excluded.status,
                heartbeat_at_utc = excluded.heartbeat_at_utc
            """,
            (run_id, started_at_utc, finished_at_utc, status, config_json,
             heartbeat_at_utc),
        )


def heartbeat_paper_run(db_path: str | Path, run_id: str, at_utc: str) -> None:
    with connect(db_path) as c:
        c.execute("UPDATE paper_runs SET heartbeat_at_utc=? WHERE run_id=?",
                   (at_utc, run_id))


def upsert_live_run(db_path: str | Path, run_id: str, *, started_at_utc: str,
                    status: str, config_json: str, account_login: int,
                    finished_at_utc: str | None = None,
                    heartbeat_at_utc: str | None = None) -> None:
    with connect(db_path) as c:
        c.execute(
            """
            INSERT INTO live_runs (run_id, started_at_utc, finished_at_utc, status,
                                    config_json, heartbeat_at_utc, account_login)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                finished_at_utc = excluded.finished_at_utc,
                status = excluded.status,
                heartbeat_at_utc = excluded.heartbeat_at_utc
            """,
            (run_id, started_at_utc, finished_at_utc, status, config_json,
             heartbeat_at_utc, account_login),
        )


def record_forced_flat(db_path: str | Path, occurred_at_utc: str, symbol: str,
                        mode: str, reason: str, mark_price: float,
                        pnl_at_close: float) -> None:
    """Audit the time-guard forced-close. Idempotent on (occurred_at, symbol).

    NB: we use ON CONFLICT DO NOTHING (not INSERT OR IGNORE) so that CHECK
    constraint violations on `mode` / `reason` still raise — only primary-key
    conflicts (the actual idempotency case) are absorbed.
    """
    with connect(db_path) as c:
        c.execute(
            "INSERT INTO forced_flat_events "
            "(occurred_at_utc, symbol, mode, reason, mark_price, pnl_at_close) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(occurred_at_utc, symbol) DO NOTHING",
            (occurred_at_utc, symbol, mode, reason, mark_price, pnl_at_close),
        )


def get_risk_state(db_path: str | Path, symbol: str, strategy: str) -> dict | None:
    with connect(db_path) as c:
        row = c.execute(
            "SELECT consecutive_losses, last_loss_at_utc, cooldown_until_utc, "
            "daily_loss_pct, day_start_balance FROM risk_state "
            "WHERE symbol=? AND strategy=?",
            (symbol, strategy),
        ).fetchone()
    if row is None:
        return None
    return {
        "symbol": symbol, "strategy": strategy,
        "consecutive_losses": row[0],
        "last_loss_at_utc": row[1],
        "cooldown_until_utc": row[2],
        "daily_loss_pct": row[3],
        "day_start_balance": row[4],
    }


def upsert_risk_state(db_path: str | Path, symbol: str, strategy: str, *,
                      consecutive_losses: int, last_loss_at_utc: str | None,
                      cooldown_until_utc: str | None, daily_loss_pct: float,
                      day_start_balance: float | None) -> None:
    with connect(db_path) as c:
        c.execute(
            """
            INSERT INTO risk_state
                (symbol, strategy, consecutive_losses, last_loss_at_utc,
                 cooldown_until_utc, daily_loss_pct, day_start_balance)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, strategy) DO UPDATE SET
                consecutive_losses = excluded.consecutive_losses,
                last_loss_at_utc = excluded.last_loss_at_utc,
                cooldown_until_utc = excluded.cooldown_until_utc,
                daily_loss_pct = excluded.daily_loss_pct,
                day_start_balance = excluded.day_start_balance
            """,
            (symbol, strategy, consecutive_losses, last_loss_at_utc,
             cooldown_until_utc, daily_loss_pct, day_start_balance),
        )


def reset_risk_state_daily(db_path: str | Path, at_utc: str,
                            day_start_balance: float) -> None:
    """FTMO 22:00 UTC daily reset: zero consecutive_losses + daily_loss_pct,
    update day_start_balance for ALL (symbol, strategy) pairs. Records the
    reset in ftmo_daily_resets. Idempotent on at_utc."""
    with connect(db_path) as c:
        c.execute(
            "INSERT OR IGNORE INTO ftmo_daily_resets (reset_at_utc, day_start_balance) "
            "VALUES (?, ?)",
            (at_utc, day_start_balance),
        )
        c.execute(
            "UPDATE risk_state "
            "SET consecutive_losses=0, daily_loss_pct=0, day_start_balance=?",
            (day_start_balance,),
        )


def record_parity_pass(db_path: str | Path, strategy: str, passed_at_utc: str,
                        divergence_dollars: float) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT OR IGNORE INTO parity_log (strategy, passed_at_utc, "
            "divergence_dollars) VALUES (?, ?, ?)",
            (strategy, passed_at_utc, divergence_dollars),
        )


def last_parity_pass(db_path: str | Path, strategy: str) -> tuple[str, float] | None:
    with connect(db_path) as c:
        row = c.execute(
            "SELECT passed_at_utc, divergence_dollars FROM parity_log "
            "WHERE strategy=? ORDER BY passed_at_utc DESC LIMIT 1",
            (strategy,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def record_bridge_event(db_path: str | Path, pinged_at_utc: str, method: str,
                         ok: bool, latency_ms: int, error: str | None = None) -> None:
    with connect(db_path) as c:
        c.execute(
            "INSERT INTO bridge_events (pinged_at_utc, method, ok, latency_ms, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (pinged_at_utc, method, 1 if ok else 0, int(latency_ms), error),
        )


def upsert_trade_note(db_path: str | Path, run_id: str, trade_idx: int, note: str,
                      updated_at_utc: str) -> None:
    with connect(db_path) as c:
        c.execute(
            """
            INSERT INTO trade_notes (trade_run_id, trade_idx, note, updated_at_utc)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(trade_run_id, trade_idx) DO UPDATE SET
                note = excluded.note,
                updated_at_utc = excluded.updated_at_utc
            """,
            (run_id, trade_idx, note, updated_at_utc),
        )
