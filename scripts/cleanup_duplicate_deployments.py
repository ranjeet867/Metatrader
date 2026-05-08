#!/usr/bin/env python3
"""cleanup_duplicate_deployments.py — remove old base-strategy-named
deployments that have a newer variant-named replacement.

Today's deployments.json has 13 entries; 5 of them are old base-strategy
slugs (e.g. `donchian_breakout_XAUUSD_H1`) that have been replaced by
variant-named cells (e.g. `donchian_55_XAUUSD_H1`). The runner sees
both → position_guard blocks one → no trades fire.

This script keeps only the newest deployment per (ticker, tf) pair,
preferring variant-named slugs over base-strategy slugs when both exist.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "accounts" / "531019095" / "deployments.json"

# Old base-strategy slugs that are now superseded
DEPRECATED_SLUGS = {
    "donchian_breakout_XAUUSD_H1",
    "rsi_meanrev_XPDUSD_H1",
    "rsi_meanrev_EURUSD_M15",
    "ema_cross_JP225_cash_M15",
    "ema_cross_HK50_cash_M15",
}


def main():
    deps = json.loads(PATH.read_text())
    print(f"Before: {len(deps)} deployments\n")

    # Backup original
    backup = PATH.with_suffix(".json.bak")
    backup.write_text(json.dumps(deps, indent=2))
    print(f"Backup saved: {backup.name}\n")

    keep = []
    removed = []
    for d in deps:
        slug = d.get("deployment_id", "")
        if slug in DEPRECATED_SLUGS:
            removed.append(d)
        else:
            keep.append(d)

    PATH.write_text(json.dumps(keep, indent=2))
    print(f"After: {len(keep)} deployments\n")
    print(f"Removed {len(removed)}:")
    for d in removed:
        print(f"  ✗ {d.get('deployment_id'):42}  status={d.get('status')}  "
              f"risk={d.get('risk_pct')}%")
    print(f"\nKept {len(keep)}:")
    for d in keep:
        print(f"  ✓ {d.get('deployment_id'):42}  status={d.get('status')}  "
              f"risk={d.get('risk_pct')}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
