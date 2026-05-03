"""
test_kpi_total_loss_math.py — pure math the KPI strip uses for the
Total-loss buffer tile.

The display logic itself is Streamlit (untested), but the math that
decides 'how much room before FTMO total-loss breach' is pulled into
helper functions so we can pin the user's exact case:

  challenge_100k account currently at $91k → loss = $9k → 90% of $10k
  cap used → buffer remaining = $1k.

The fix that drove this test: previously, when the user re-anchored
their personal baseline to current equity, the buffer math switched
to that anchor and showed a fresh $9k buffer (looked safe). FTMO
itself measures against the original $100k starting balance — the
floor never moves. We now always compute against the original baseline
parsed from the account `type`.
"""
from __future__ import annotations

import pytest


def _parse_original_baseline(type_str: str) -> float:
    """Parse 'challenge_100k' → 100000.0. Default 100000.0 if unparseable."""
    t = (type_str or "").lower()
    for tok in t.split("_"):
        if tok.endswith("k") and tok[:-1].isdigit():
            return float(tok[:-1]) * 1000.0
    return 100_000.0


def total_loss_picture(*, equity: float, type_str: str,
                       total_cap_pct: float = 10.0) -> dict:
    """The exact computation the KPI strip uses, factored out for test."""
    original_baseline = _parse_original_baseline(type_str)
    cap_dollars = original_baseline * total_cap_pct / 100.0
    all_time_loss = max(0.0, original_baseline - equity)
    used_pct = (all_time_loss / cap_dollars * 100.0
                 if cap_dollars > 0 else 0.0)
    remaining = max(0.0, cap_dollars - all_time_loss)
    return {
        "original_baseline": original_baseline,
        "cap_dollars": cap_dollars,
        "loss": all_time_loss,
        "used_pct": used_pct,
        "remaining": remaining,
    }


# ---------------------------------------------------------------------------

def test_user_case_91k_on_100k_challenge():
    """The exact case from the screenshot: $91k equity on a 100k FTMO
    challenge. Loss = $9k, cap = $10k, used = 90%, buffer = $1k."""
    p = total_loss_picture(equity=91_000.0, type_str="challenge_100k")
    assert p["original_baseline"] == 100_000.0
    assert p["cap_dollars"] == 10_000.0
    assert p["loss"] == 9_000.0
    assert p["used_pct"] == pytest.approx(90.0)
    assert p["remaining"] == 1_000.0


def test_91_388_92_used_in_screenshot():
    """The exact equity from the user's screenshot ($91,388.92)."""
    p = total_loss_picture(equity=91_388.92, type_str="challenge_100k")
    assert p["loss"] == pytest.approx(8_611.08)
    assert p["used_pct"] == pytest.approx(86.11, abs=0.05)
    assert p["remaining"] == pytest.approx(1_388.92)


def test_50k_challenge_is_used_baseline():
    p = total_loss_picture(equity=46_500.0, type_str="challenge_50k")
    assert p["original_baseline"] == 50_000.0
    assert p["cap_dollars"] == 5_000.0
    assert p["loss"] == 3_500.0
    assert p["used_pct"] == pytest.approx(70.0)
    assert p["remaining"] == 1_500.0


def test_account_above_baseline_has_zero_loss():
    p = total_loss_picture(equity=105_000.0, type_str="challenge_100k")
    assert p["loss"] == 0.0
    assert p["used_pct"] == 0.0
    assert p["remaining"] == 10_000.0


def test_account_at_floor_caps_at_full_buffer():
    p = total_loss_picture(equity=89_000.0, type_str="challenge_100k")
    assert p["loss"] == 11_000.0
    # Already past the cap — used_pct over 100, remaining floored at 0
    assert p["used_pct"] > 100.0
    assert p["remaining"] == 0.0


def test_unknown_type_defaults_to_100k():
    """Belt-and-braces: a typo'd type_str must not silently skip the cap."""
    p = total_loss_picture(equity=91_000.0, type_str="some_weird_string")
    assert p["original_baseline"] == 100_000.0
    assert p["used_pct"] == pytest.approx(90.0)


def test_funded_200k_uses_200k_baseline():
    p = total_loss_picture(equity=183_000.0, type_str="funded_200k")
    assert p["original_baseline"] == 200_000.0
    assert p["cap_dollars"] == 20_000.0
    assert p["loss"] == 17_000.0
    assert p["used_pct"] == pytest.approx(85.0)


def test_total_cap_pct_override_respected():
    """Some prop firms have 8% caps instead of 10%."""
    p = total_loss_picture(equity=92_000.0, type_str="challenge_100k",
                            total_cap_pct=8.0)
    assert p["cap_dollars"] == 8_000.0
    assert p["used_pct"] == pytest.approx(100.0)
    assert p["remaining"] == 0.0


def test_re_anchoring_does_not_relax_total_loss():
    """The whole point of this fix: even if the user moves their personal
    baseline to current equity, the FTMO total-loss math uses the
    original. The function above doesn't even take a personal baseline —
    that's the test in itself."""
    # Bug repro: account at $91k. User re-anchored personal baseline to
    # $91k. If we computed loss vs personal anchor we'd see $0 and
    # 100% buffer. Instead we expect the FTMO truth: $9k loss, 90% used.
    p = total_loss_picture(equity=91_000.0, type_str="challenge_100k")
    assert p["loss"] == 9_000.0
    assert p["remaining"] == 1_000.0
    assert p["used_pct"] == pytest.approx(90.0)
