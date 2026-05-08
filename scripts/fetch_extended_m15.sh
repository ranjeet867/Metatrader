#!/usr/bin/env bash
# fetch_extended_m15.sh — try to pull as much M15 history as the broker
# will give us for the top backtest-relevant tickers.
#
# Run this on YOUR machine (where MT5 + the file-bridge are connected),
# not in any sandbox. The bridge talks directly to MetaTrader.
#
# What it does:
#   1. Calls scripts/load_real_data.py with --bars 100000 (~3.5y M15)
#   2. Brokers typically cap M15 at 50k-200k bars
#   3. Shows you exactly how much came back per ticker
#
# Usage:
#   bash scripts/fetch_extended_m15.sh
#
# Time: ~30-90s per ticker depending on broker. Total ~5-10 minutes.
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYBIN="${REPO}/.venv/bin/python"

# Tickers we actively backtest on M15 — extend or trim as needed
TICKERS=(
  "EURUSD"
  "US100.cash"
  "JP225.cash"
  "HK50.cash"
  "XAUUSD"
  "XPDUSD"
  "XPTUSD"
  "XAGUSD"
  "US500.cash"
  "US30.cash"
)

BARS=150000    # 150k M15 ≈ 5y target. Broker may cap at 50k-100k.
TIMEOUT=600    # 10 min per ticker. 150k bars = ~150s serialize + transfer overhead.

echo "============================================================"
echo "Fetching extended M15 history (target: ${BARS} bars, timeout=${TIMEOUT}s)"
echo "============================================================"
echo ""

ok=0
total=${#TICKERS[@]}

for tic in "${TICKERS[@]}"; do
  echo "→ ${tic} M15 ..."
  if "${PYBIN}" "${REPO}/scripts/load_real_data.py" \
       --ticker "${tic}" --tf M15 --bars "${BARS}" --timeout "${TIMEOUT}" 2>&1 | tail -4; then
    ok=$((ok + 1))
  else
    echo "  ⚠ failed (likely broker doesn't have this many M15 bars; "
    echo "      try smaller --bars or check MT5 chart's available history)"
  fi
  echo ""
done

echo "============================================================"
echo "DONE — ${ok}/${total} tickers fetched"
echo "============================================================"
echo ""
echo "Now check what you got back:"
echo "  ${PYBIN} -c \""
echo "import pandas as pd"
echo "from pathlib import Path"
echo "from core.data import load_parquet"
echo "for f in sorted(Path('data').glob('*_M15.parquet')):"
echo "    df = load_parquet(f)"
echo "    t0, t1 = pd.to_datetime(df['time'].iloc[0]).date(), pd.to_datetime(df['time'].iloc[-1]).date()"
echo "    yrs = (t1-t0).days / 365.25"
echo "    print(f'{f.name:30} {len(df):>9,} bars  {t0} → {t1}  {yrs:.2f}y')"
echo "  \""
