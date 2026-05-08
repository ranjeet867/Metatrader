#!/usr/bin/env python3
"""
migrate_use_container_width.py — replace deprecated
`use_container_width=True/False` kwargs with the new `width=...`
form per Streamlit's 2026-01 deprecation timeline.

Mapping:
  use_container_width=True   →  width="stretch"
  use_container_width=False  →  width="content"

Skips:
  - .pyc / __pycache__
  - .bak files
  - This script itself
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = [ROOT / "dashboards"]
SKIP_PATHS = {ROOT / "scripts" / "migrate_use_container_width.py"}


def _migrate_text(text: str) -> tuple[str, int]:
    """Return (new_text, n_replacements)."""
    n = 0
    # use_container_width=True → width="stretch"
    new, k = re.subn(
        r"\buse_container_width\s*=\s*True\b",
        'width="stretch"',
        text,
    )
    n += k
    # use_container_width=False → width="content"
    new, k = re.subn(
        r"\buse_container_width\s*=\s*False\b",
        'width="content"',
        new,
    )
    n += k
    return new, n


def main(apply: bool = False) -> int:
    files_changed = 0
    total_replacements = 0
    for base in SCAN_DIRS:
        for path in base.rglob("*.py"):
            if "__pycache__" in str(path):
                continue
            if path.suffix == ".bak":
                continue
            if path in SKIP_PATHS:
                continue
            text = path.read_text()
            new_text, n = _migrate_text(text)
            if n > 0:
                if apply:
                    path.write_text(new_text)
                rel = str(path.relative_to(ROOT))
                action = "FIXED" if apply else "would fix"
                print(f"  {action} {rel:60} ({n} occurrences)")
                files_changed += 1
                total_replacements += n
    print()
    print(f"{'Files changed' if apply else 'Files needing change'}: "
          f"{files_changed}")
    print(f"Total replacements: {total_replacements}")
    if not apply:
        print()
        print("Re-run with --apply to actually edit the files.")
    return 0


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    sys.exit(main(apply=apply))
