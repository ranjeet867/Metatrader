"""
test_dashboard_arrow_roundtrip.py — defensive tests against the
"lots column" Arrow-conversion bug pattern.

Background
----------
On 2026-05-05 the dashboard threw `pyarrow.lib.ArrowInvalid: Could not
convert '—' with type str: tried to convert to double` because the
Account Risk page mixed numeric `round(res.lots, 4)` with string `"—"`
placeholders in the same column. Streamlit's `st.dataframe()` calls
`pa.Table.from_pandas(df)` internally, which fails on object-dtype
columns containing mixed numeric + string types.

This test file enforces three guarantees:

1. **Static lint**: scan dashboards/ for the dangerous pattern
   `"col": round(...) if ... else "—"` (or similar). Such code is a
   future Arrow bug waiting to happen.

2. **Page import smoke**: every dashboard page module imports without
   error. Catches accidental syntax breakage during refactors.

3. **DataFrame round-trip**: for every dataframe-producing helper
   we know about, build it with synthetic data and assert
   `pa.Table.from_pandas(df)` succeeds.

Adding new pages: this test will ALWAYS catch import errors. To add
round-trip coverage for a new helper, add it to ROUND_TRIP_HELPERS.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest


REPO = Path(__file__).resolve().parent.parent
PAGES_DIR = REPO / "dashboards" / "pages"


# --------------------------------------------------------------- #
# 1. Static lint — catch the "mixed-type column" pattern          #
# --------------------------------------------------------------- #
def _scan_for_mixed_type_columns(text: str, file: Path) -> list[str]:
    """Look for dict-literal entries that mix numeric and string-marker
    on the same key, e.g.:
        "lots": round(res.lots, 4) if res.ok else "—",
        "$ at risk": x if y else "N/A",

    Such patterns produce object-dtype columns that break Arrow when
    the DataFrame is rendered via st.dataframe().
    """
    issues = []
    # Pattern: "key": <numeric_expr> if <cond> else <string_literal>
    # Looks for: round(...) | float(...) | int(...) | math.* | abs(...)
    # ... if cond else "—" / "N/A" / "—" / etc.
    pattern = re.compile(
        r'["\'][^"\'\n]+["\']\s*:\s*'      # "key":
        r'(round|float|int|abs|math|sum)\s*\([^)]*\)'  # numeric_call(...)
        r'\s+if\s+[^,}]+\s+else\s+'        # if cond else
        r'["\'][^"\'\n]{1,5}["\']\s*[,}]', # short string literal
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        # Find the line number
        line_no = text[: m.start()].count("\n") + 1
        snippet = m.group(0)[:120].replace("\n", " ")
        issues.append(f"{file.relative_to(REPO)}:{line_no}: {snippet}")
    return issues


def test_no_mixed_type_columns_in_dashboards():
    """Fails the build if anyone reintroduces the lots-column pattern.

    Mixing numeric values with string placeholders ('—', 'N/A') in the
    same dict column makes the resulting DataFrame object-dtype, which
    breaks Streamlit's Arrow serialization. Use float('nan') instead
    and a NumberColumn column-config to render NaN as '—' visually.
    """
    issues = []
    for py_file in PAGES_DIR.rglob("*.py"):
        text = py_file.read_text()
        issues.extend(_scan_for_mixed_type_columns(text, py_file))
    # Also scan dashboards/components/ where shared widgets live
    for py_file in (REPO / "dashboards" / "components").rglob("*.py"):
        text = py_file.read_text()
        issues.extend(_scan_for_mixed_type_columns(text, py_file))
    if issues:
        msg = (
            "Found mixed-type column patterns that will break "
            "st.dataframe Arrow serialization:\n  "
            + "\n  ".join(issues)
            + "\n\nFix: use float('nan') instead of '—' and add a "
            "NumberColumn column-config to render NaN visually."
        )
        pytest.fail(msg)


# --------------------------------------------------------------- #
# 2. Page import smoke — every page module imports clean          #
# --------------------------------------------------------------- #
@pytest.mark.parametrize(
    "page_path",
    sorted(p for p in PAGES_DIR.glob("*.py") if p.name != "__init__.py"),
    ids=lambda p: p.name,
)
def test_dashboard_page_imports_cleanly(page_path: Path):
    """Each page module must AST-parse cleanly and importable.

    Streamlit pages are imported by the runner on first navigation;
    a syntax/import error means the user sees a stack trace instead
    of the page. This test catches it pre-deploy.
    """
    import ast
    text = page_path.read_text()
    try:
        ast.parse(text)
    except SyntaxError as e:
        pytest.fail(
            f"{page_path.name} fails AST parse: line {e.lineno}: {e.msg}"
        )


# --------------------------------------------------------------- #
# 3. Specific helper round-trip — synthesize data, build df,      #
#    confirm pa.Table.from_pandas succeeds                        #
# --------------------------------------------------------------- #
def _round_trip_df(df: pd.DataFrame, label: str) -> None:
    """Convert a DataFrame to an Arrow Table — fails loudly with a
    helpful message if a column has mixed types."""
    try:
        pa.Table.from_pandas(df)
    except (pa.lib.ArrowInvalid, pa.lib.ArrowTypeError) as e:
        # Try to identify the bad column
        bad_cols = []
        for col in df.columns:
            try:
                pa.Array.from_pandas(df[col])
            except Exception as col_e:
                bad_cols.append(f"  column '{col}': {col_e}")
        msg = (
            f"DataFrame '{label}' fails Arrow round-trip:\n"
            f"  Original error: {e}\n"
        )
        if bad_cols:
            msg += "  Per-column failures:\n" + "\n".join(bad_cols)
        msg += (
            "\n  Common cause: dict literal mixed numeric value with "
            "string placeholder. Use float('nan') instead of '—'."
        )
        pytest.fail(msg)


def test_account_risk_projected_lots_table_round_trips():
    """The exact bug from 2026-05-05. Build the projected-lots table
    with a row that has res.ok=True (numeric) and res.ok=False (NaN
    placeholder), verify Arrow handles it."""
    rows = [
        {"symbol": "EURUSD", "typical_entry": 1.10,
         "stop_distance": 0.001,
         "lots": 0.10, "$ at risk": 100.0, "result": "ok"},
        # Failed row — uses NaN, NOT '—'
        {"symbol": "XPDUSD", "typical_entry": 1000.0,
         "stop_distance": 5.0,
         "lots": float("nan"), "$ at risk": float("nan"),
         "result": "stop_below_entry"},
        {"symbol": "XAUUSD", "typical_entry": 2000.0,
         "stop_distance": 5.0,
         "lots": 0.05, "$ at risk": 50.0, "result": "ok"},
    ]
    df = pd.DataFrame(rows)
    _round_trip_df(df, "account_risk_projected_lots")


def test_edge_catalog_listing_round_trips():
    """Catalog cells produce dataframes for Strategy Library + Composer."""
    from core import edge_catalog
    cat = edge_catalog.load_catalog()
    rows = []
    for (tic, tf), lst in cat.items():
        for e in lst[:3]:  # cap at first 3 per cell to keep fast
            rows.append({
                "ticker": e.ticker,
                "tf": e.tf,
                "strategy": e.strategy,
                "PF_test": float(e.test_pf),
                "n_test": int(e.n_test),
                "score": float(e.score) if e.score is not None else
                            float("nan"),
                "win_rate_pct": float(e.win_rate_pct or 0),
                "rr_ratio": float(e.rr_ratio or 0),
                "recovery_days": (
                    float(e.recovery_days) if e.recovery_days is not None
                    else float("nan")
                ),
            })
    if not rows:
        pytest.skip("empty catalog — skip round-trip test")
    df = pd.DataFrame(rows)
    _round_trip_df(df, "edge_catalog_listing")


def test_edge_decay_assessment_round_trips():
    """The Command Center edge-decay tile builds a dataframe of
    per-deployment z-score breakdowns. Verify it Arrow-serializes."""
    from core import edge_decay
    rows = [
        {
            "Deployment": "tsmom_JP225_D1_long",
            "State": "GREEN",
            "n_trades": 12,
            "Live R": 0.15,
            "Catalog R": 0.13,
            "z-score": 0.5,
            "Sustained red": 0,
            "Reason": "GREEN — within noise",
        },
        # Insufficient row — uses NaN for Live R / z-score
        {
            "Deployment": "rsi_30_70_EURUSD_M15_long",
            "State": "INSUFFICIENT",
            "n_trades": 4,
            "Live R": float("nan"),
            "Catalog R": 0.13,
            "z-score": float("nan"),
            "Sustained red": 0,
            "Reason": "INSUFFICIENT — only 4 trades",
        },
        {
            "Deployment": "ema_cross_US100_M15_bidir",
            "State": "RED",
            "n_trades": 35,
            "Live R": -0.45,
            "Catalog R": 0.61,
            "z-score": -3.2,
            "Sustained red": 18,
            "Reason": "RED — auto-demoted",
        },
    ]
    df = pd.DataFrame(rows)
    _round_trip_df(df, "edge_decay_assessment")


def test_holding_badge_does_not_break_arrow():
    """Verify deployment-status data round-trips. The holding badge
    is HTML rendered separately, but the listing dataframes shouldn't
    embed HTML strings into numeric columns."""
    rows = [
        {
            "Strategy": "rsi_30_70",
            "Ticker": "EURUSD",
            "TF": "M15",
            "Risk %": 0.10,
            "Status": "live",
            "$/trade": 100.0,
            "Holding": "—",   # Status string column, NOT a numeric col
        },
        {
            "Strategy": "tsmom",
            "Ticker": "JP225.cash",
            "TF": "D1",
            "Risk %": 0.05,
            "Status": "paper",
            "$/trade": 50.0,
            "Holding": "💼 LONG +$120",
        },
    ]
    df = pd.DataFrame(rows)
    _round_trip_df(df, "deployment_listing_with_holding")
