#!/usr/bin/env python3
"""check_data_freshness.py — for every (ticker, tf) referenced by an
active deployment, report:
  - last bar timestamp (UTC)
  - lag from now
  - bar count
  - gap count in the last N bars (M15/H1 should have no gaps during
    trading hours; D1 has weekend gaps which we tolerate)

Run: cd <repo> && .venv/bin/python scripts/check_data_freshness.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
from core.data import load_parquet


# Allowed lag in minutes per TF, before we flag the row.
# Tolerances are generous because broker server time vs UTC differs
# by ±3h depending on broker; we only want to catch GENUINE staleness
# (e.g. parquet hasn't been touched in many TF periods).
TF_TOL_MIN = {"M15": 30, "H1": 75, "D1": 1500}

# Expected bar interval in minutes (for gap detection)
TF_INTERVAL_MIN = {"M15": 15, "H1": 60, "D1": 1440}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _normalise_ts(ts) -> datetime:
    """Normalise a parquet 'time' value to a UTC datetime. Handles both
    tz-aware and naive (assumed UTC) timestamps."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime()


def _check_one(ticker: str, tf: str, deps: list[str]) -> dict:
    p = ROOT / "data" / f"{ticker}_{tf}.parquet"
    if not p.exists():
        return {"ticker": ticker, "tf": tf, "status": "MISSING_FILE",
                "deps": deps}
    try:
        df = load_parquet(p)
    except Exception as e:
        return {"ticker": ticker, "tf": tf, "status": f"LOAD_ERR: {e}",
                "deps": deps}

    n = len(df)
    last_ts = _normalise_ts(df["time"].iloc[-1])
    first_ts = _normalise_ts(df["time"].iloc[0])
    now = _now_utc()
    lag = (now - last_ts).total_seconds() / 60   # minutes

    tol = TF_TOL_MIN.get(tf, 60)
    if lag < 0:
        # Last bar is in the future relative to UTC — usually a broker-
        # timezone display issue (server time UTC+3, etc.). Treat as fresh.
        status = "FRESH (broker tz offset)"
    elif lag <= tol:
        status = "FRESH"
    elif lag <= tol * 2:
        status = "STALE"
    else:
        status = "VERY STALE"

    # Gap detection in last 50 bars (M15/H1) or 30 (D1).
    interval = TF_INTERVAL_MIN.get(tf, 60)
    n_check = 50 if tf in ("M15", "H1") else 30
    recent = df.tail(n_check + 1).copy()
    recent["time_utc"] = recent["time"].apply(_normalise_ts)
    recent["delta_min"] = (
        recent["time_utc"].diff().dt.total_seconds() / 60
    )
    # Flag deltas > 1.5x interval as gaps (tolerates session boundaries).
    threshold = interval * 1.5
    gaps = recent[recent["delta_min"] > threshold]
    # For D1, weekend gaps (3 days = 4320 min) are NORMAL — exclude them.
    if tf == "D1":
        gaps = gaps[gaps["delta_min"] > 4500]

    return {
        "ticker": ticker, "tf": tf,
        "status": status, "deps": deps, "n": n,
        "last_ts": last_ts.isoformat()[:19],
        "first_ts": first_ts.isoformat()[:19],
        "lag_min": lag,
        "n_recent_gaps": len(gaps),
        "max_gap_min": (gaps["delta_min"].max()
                          if len(gaps) > 0 else None),
    }


def main() -> int:
    # 1. Load all active deployments
    dep_path = ROOT / "data/accounts/531019095/deployments.json"
    if not dep_path.exists():
        print(f"deployments.json not found at {dep_path}")
        return 1
    with open(dep_path) as f:
        deps = json.load(f)
    active = [d for d in deps if d.get("status") in ("paper", "live")]
    print(f"Active deployments: {len(active)} "
          f"(paper={sum(1 for d in active if d['status']=='paper')}, "
          f"live={sum(1 for d in active if d['status']=='live')})")
    print()

    # 2. Group by (ticker, tf)
    by_key: dict[tuple[str, str], list[str]] = {}
    for d in active:
        key = (d["ticker"], d["tf"])
        by_key.setdefault(key, []).append(
            f"{d['status'][0].upper()}: {d['deployment_id']}"
        )

    # 3. Check each
    results = [_check_one(t, tf, ds) for (t, tf), ds in by_key.items()]
    results.sort(key=lambda r: (r["tf"], r["ticker"]))

    # 4. Print
    flag = {"FRESH": "✅", "FRESH (broker tz offset)": "✅",
              "STALE": "⚠️ ", "VERY STALE": "🚫", "MISSING_FILE": "🚫"}
    print(f"{'':3} {'Ticker':14} {'TF':4} {'Bars':>7} "
          f"{'Lag(m)':>10} {'Gaps':>5} {'Last bar (UTC)':19}")
    print("─" * 80)
    n_stale = 0
    for r in results:
        f = flag.get(r["status"], "❓")
        if "STALE" in r["status"] or r["status"] == "MISSING_FILE":
            n_stale += 1
        lag = r.get("lag_min", 0)
        lag_str = (f"{lag:+.0f}m" if abs(lag) < 60
                    else f"{lag/60:+.1f}h")
        gaps = r.get("n_recent_gaps", 0)
        gap_str = f"{gaps}" + ("⚠" if gaps > 0 else "")
        last = r.get("last_ts", "—")
        print(f"{f:3} {r['ticker']:14} {r['tf']:4} {r.get('n',0):>7,} "
              f"{lag_str:>10} {gap_str:>5} {last:19}")
        # Show big gaps inline
        if r.get("max_gap_min") and r["max_gap_min"] > 0:
            interval = TF_INTERVAL_MIN.get(r["tf"], 60)
            print(f"       ↳ largest recent gap: "
                  f"{r['max_gap_min']:.0f}m "
                  f"({r['max_gap_min']/interval:.1f}× interval)")

    print()
    if n_stale == 0:
        print("All parquets fresh ✅")
    else:
        print(f"⚠️  {n_stale} stale / missing parquet(s) — flagged above")

    # 5. Mismatch check: any deployment ticker/tf without a parquet?
    print()
    print("=" * 80)
    print("DEPLOYMENT → PARQUET COVERAGE")
    print("=" * 80)
    for r in results:
        for dep_id in r["deps"]:
            covered = r["status"] != "MISSING_FILE"
            tag = "✅" if covered else "🚫"
            print(f"  {tag} {dep_id:60} {r['ticker']:12} {r['tf']:4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
