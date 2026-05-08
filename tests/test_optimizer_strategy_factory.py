"""
test_optimizer_strategy_factory.py — regression test for the bbands kwargs
crash and any future param-name drift.

Bug history:
  optimize_portfolio.py was passing `period=20, num_std=2.0` to
  BBandsMeanRevParams, which expects `bb_period, bb_k`. The dataclass
  raised TypeError and the optimizer's broad try/except silently
  printed `skip bbands_20_2 ...: unexpected keyword argument 'period'`.

  Result: bbands_20_2 was never tested in any optimizer run, on any
  ticker, on any TF. The "skip" warning was easy to miss in 100s of
  lines of grid output.

This test instantiates every strategy in STRATEGY_NAMES with both
long_only=True and long_only=False to confirm the factory closures
don't crash on default args.
"""
from __future__ import annotations

import pytest

from scripts.optimize_portfolio import STRATEGY_NAMES, _build_rr_aware


@pytest.mark.parametrize("name", STRATEGY_NAMES)
@pytest.mark.parametrize("long_only", [True, False])
def test_strategy_factory_instantiates(name: str, long_only: bool) -> None:
    """Each strategy must build cleanly with default kwargs in both
    bidirectional and long-only modes. If a param name drifts, this
    fires before the optimizer's broad except catches it."""
    variants = _build_rr_aware(name)
    assert variants, f"no variants for {name}"
    for label, factory in variants:
        instance = factory(long_only)
        assert instance is not None
        assert hasattr(instance, "name") or hasattr(instance, "signals"), (
            f"{name}/{label} produced something that's not a strategy"
        )


def test_bbands_uses_correct_param_names() -> None:
    """Specifically pin the bbands kwargs — bb_period not period,
    bb_k not num_std. This is the exact bug we fixed."""
    variants = _build_rr_aware("bbands_20_2")
    assert len(variants) == 1
    label, factory = variants[0]
    inst = factory(False)
    # Inspect the params dataclass — must have bb_period=20, bb_k=2.0
    p = inst.params
    assert p.bb_period == 20, f"expected bb_period=20, got {p.bb_period}"
    assert p.bb_k == 2.0, f"expected bb_k=2.0, got {p.bb_k}"
    assert not hasattr(p, "period"), (
        "Params accidentally has a 'period' attr — kwargs naming "
        "drift detected. Use bb_period."
    )


def test_strategy_names_cover_all_known_strategies() -> None:
    """If we add a new strategy, STRATEGY_NAMES needs to know about it.
    Pin the current set so additions are deliberate."""
    expected = {
        "ema_cross_9_20", "ema_cross_12_26",
        "donchian_20", "donchian_55",
        "ema_pullback_20_50", "rsi_30_70", "bbands_20_2",
    }
    assert set(STRATEGY_NAMES) == expected, (
        f"STRATEGY_NAMES drifted: {sorted(STRATEGY_NAMES)} "
        f"vs {sorted(expected)}"
    )
