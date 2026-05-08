#!/usr/bin/env python3
"""
refresh_news_calendar.py — pull the ForexFactory weekly calendar
into data/news_calendar.json. Run daily via cron / LaunchAgent.

Usage:
  python scripts/refresh_news_calendar.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import news_calendar


def main() -> int:
    cache = ROOT / "data" / "news_calendar.json"
    n = news_calendar.refresh_cache(cache)
    print(f"✅ Refreshed news calendar — {n} events written to {cache}")
    if n == 0:
        print("⚠ Zero events — feed may be down or weekend (no upcoming "
              "events scheduled). Check tomorrow.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
