#!/usr/bin/env python3
"""
build_edge_tracker.py — group optimizer output by ticker.

The optimizer writes a flat leaderboard sorted by score. That's good for
"what are the strongest cells overall" but not for "what edge do I have
on each ticker." This script re-pivots the data:

  for each ticker:
    list every surviving (strategy, tf, R:R) cell ordered by score
    flag the champion
    summarise: best score, best test_R, count of survivors

Output: docs/edge_tracker.md — one section per ticker, with a top-line
summary table at the start.

Usage:
    python scripts/build_edge_tracker.py
    python scripts/build_edge_tracker.py --in docs/optimization_full_2026-05-03_1918.md
    python scripts/build_edge_tracker.py --out docs/edge_tracker.md
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Cell:
    rank: int
    strategy: str
    ticker: str
    tf: str
    rr: str
    n: int
    pf: float
    r: float
    win_pct: float
    max_dd_pct: float
    recov_d: str
    streak: int
    rr_ratio: float
    cagr: str
    p_pass: str
    sustained: bool
    score: float


def _split_pipe(line: str) -> list[str]:
    """Split a markdown table row into trimmed cells, dropping the
    leading/trailing empty cells from `|...|`."""
    if not line.startswith("|"):
        return []
    return [c.strip() for c in line.strip("|").split("|")]


def parse(md_path: Path) -> list[Cell]:
    """Parse rows out of either the legacy 17-col or the wide 23-col
    markdown table. We only need a few fields for the per-ticker
    tracker, so we just pull cells positionally."""
    out: list[Cell] = []
    in_table = False
    cols = 0
    for line in md_path.read_text().splitlines():
        if line.startswith("|") and "ticker" in line and "tf" in line:
            cols = len(_split_pipe(line))
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table:
            cells = _split_pipe(line)
            if not cells or len(cells) != cols:
                in_table = False
                continue
            try:
                rank = int(cells[0])
                strat = cells[1].strip("` ")
                tic = cells[2].strip("` ")
                tf = cells[3].strip("` ")
                if cols == 23:
                    # rank|strategy|ticker|tf|side|R:R|n|PF|R|win%|netPnL$
                    # |maxDD%|maxDD$|DDdays|recovD|streak|rr|avgWin$
                    # |avgLoss$|CAGR|P(pass)|sus|score
                    side = cells[4]
                    rr_lbl = cells[5]
                    n = int(cells[6])
                    pf = float(cells[7])
                    r = float(cells[8])
                    win = float(cells[9])
                    dd_pct = float(cells[11])
                    recov = cells[14]
                    streak = int(cells[15])
                    rr_ratio = float(cells[16])
                    cagr = cells[19]
                    p_pass = cells[20]
                    sus_str = cells[21]
                    score = float(cells[22])
                else:
                    # Legacy 17-col
                    side = "long"
                    rr_lbl = cells[4]
                    n = int(cells[5])
                    pf = float(cells[6])
                    r = float(cells[7])
                    win = float(cells[8])
                    dd_pct = float(cells[9])
                    recov = cells[10]
                    streak = int(cells[11])
                    rr_ratio = float(cells[12])
                    cagr = cells[13]
                    p_pass = cells[14]
                    sus_str = cells[15]
                    score = float(cells[16])
                out.append(Cell(
                    rank=rank,
                    strategy=f"{strat} ({side})" if side != "long" else strat,
                    ticker=tic, tf=tf, rr=rr_lbl,
                    n=n, pf=pf, r=r, win_pct=win, max_dd_pct=dd_pct,
                    recov_d=recov, streak=streak, rr_ratio=rr_ratio,
                    cagr=cagr, p_pass=p_pass,
                    sustained="✅" in sus_str, score=score,
                ))
            except (ValueError, IndexError):
                continue
    return out


def group_by_ticker(cells: list[Cell]) -> dict[str, list[Cell]]:
    by_ticker: dict[str, list[Cell]] = defaultdict(list)
    for c in cells:
        by_ticker[c.ticker].append(c)
    for ticker in by_ticker:
        by_ticker[ticker].sort(key=lambda c: -c.score)
    return dict(by_ticker)


def render_summary_table(by_ticker: dict[str, list[Cell]]) -> str:
    """Top-line: one row per ticker showing best champion."""
    rows = []
    for ticker, cells in sorted(by_ticker.items(),
                                  key=lambda kv: -kv[1][0].score):
        champ = cells[0]
        n_surv = len(cells)
        rows.append((ticker, n_surv, champ))

    lines = [
        "| ticker | survivors | champion strategy | tf | R:R | test_R | PF | P(pass) | score |",
        "|---|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for ticker, n_surv, c in rows:
        lines.append(
            f"| `{ticker}` | {n_surv} "
            f"| `{c.strategy}` | `{c.tf}` | {c.rr} "
            f"| {c.r:+.2f} | {c.pf:.2f} | {c.p_pass} | {c.score:.1f} |"
        )
    return "\n".join(lines)


def render_per_ticker(ticker: str, cells: list[Cell]) -> str:
    """Detailed leaderboard for one ticker."""
    out = [f"\n## `{ticker}`  ·  {len(cells)} surviving cells\n"]
    out.append(
        "| # | strategy | tf | R:R | n | PF | test_R | win% | maxDD% | "
        "rr | P(pass) | sus | score |"
    )
    out.append(
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    )
    for i, c in enumerate(cells[:15], 1):
        sus = "✅" if c.sustained else "—"
        out.append(
            f"| {i} | `{c.strategy}` | `{c.tf}` | {c.rr} "
            f"| {c.n} | {c.pf:.2f} | {c.r:+.2f} | {c.win_pct:.0f} "
            f"| {c.max_dd_pct:.1f} | {c.rr_ratio:.2f} "
            f"| {c.p_pass} | {sus} | {c.score:.1f} |"
        )
    if len(cells) > 15:
        out.append(f"\n*({len(cells) - 15} more cells omitted)*")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=None,
                     help="Input optimization markdown (default: latest)")
    ap.add_argument("--out", default="docs/edge_tracker.md",
                     help="Output path")
    args = ap.parse_args()

    if args.inp:
        p = Path(args.inp)
        in_path = p if p.is_absolute() else (ROOT / p).resolve()
    else:
        candidates = sorted((ROOT / "docs").glob("optimization_*.md"))
        if not candidates:
            print("error: no docs/optimization_*.md found", file=sys.stderr)
            return 1
        in_path = candidates[-1]
    print(f"reading {in_path}")

    cells = parse(in_path)
    if not cells:
        print("error: no cells parsed (regex mismatch?)", file=sys.stderr)
        return 1
    print(f"parsed {len(cells)} cells")

    by_ticker = group_by_ticker(cells)
    print(f"covered {len(by_ticker)} tickers")

    try:
        src_label = str(in_path.relative_to(ROOT))
    except ValueError:
        src_label = in_path.name
    out_lines = [
        "# Edge tracker — per-ticker leaderboards\n",
        f"Source: `{src_label}`  ·  {len(cells)} survivor cells "
        f"across {len(by_ticker)} tickers\n",
        "## Champions per ticker\n",
        "Ranked by champion-cell score. `survivors` = how many "
        "(strategy × tf × R:R) cells survived for this ticker.\n",
        render_summary_table(by_ticker),
        "\n---\n",
        "## Detailed leaderboards\n",
    ]
    for ticker in sorted(by_ticker, key=lambda t: -by_ticker[t][0].score):
        out_lines.append(render_per_ticker(ticker, by_ticker[ticker]))

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_lines))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
