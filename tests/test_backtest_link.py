"""
test_backtest_link.py — verify the shared /Backtest URL builder
embeds the catalog row's full config so a round-trip Composer →
Backtest reproduces the exact metrics.
"""
from __future__ import annotations

import base64
import json

from dashboards.components.backtest_link import (
    build_backtest_url, build_backtest_url_from_parts,
)
from core.edge_catalog import EdgeStat


def _make_edge(config_json: str = "") -> EdgeStat:
    return EdgeStat(
        ticker="EURUSD", tf="M15", strategy="rsi_30_70",
        n_train=60, train_pf=1.5, train_r=0.2,
        n_test=42, test_pf=1.62, test_r=0.228,
        source_config_json=config_json,
    )


def test_url_includes_ticker_tf_strategy():
    url = build_backtest_url(_make_edge())
    assert "ticker=EURUSD" in url
    assert "tf=M15" in url
    assert "strategy=rsi_30_70" in url


def test_url_omits_config_when_no_source():
    url = build_backtest_url(_make_edge(config_json=""))
    assert "config=" not in url


def test_url_embeds_config_when_present():
    cfg = {"oversold": 30, "overbought": 70,
           "stop_atr_mult": 1.5, "target_atr_mult": 1.5}
    url = build_backtest_url(_make_edge(json.dumps(cfg)))
    assert "config=" in url
    # Round-trip: decode the config param and confirm it matches
    blob = url.split("config=")[1]
    pad = "=" * (-len(blob) % 4)
    decoded = json.loads(
        base64.urlsafe_b64decode((blob + pad).encode("ascii"))
              .decode("utf-8")
    )
    assert decoded == cfg


def test_relative_url_has_no_host():
    url = build_backtest_url(_make_edge(), relative=True)
    assert url.startswith("/Backtest?")
    assert "localhost" not in url


def test_absolute_url_has_default_host():
    url = build_backtest_url(_make_edge())
    assert url.startswith("http://localhost:8502/Backtest?")


def test_from_parts_helper_works_for_non_edge_callers():
    cfg_json = '{"period": 55}'
    url = build_backtest_url_from_parts(
        ticker="XAUUSD", tf="H1", strategy="donchian_55",
        source_config_json=cfg_json,
    )
    assert "ticker=XAUUSD" in url
    assert "config=" in url


def test_round_trip_lossless_for_all_strategy_types():
    """Every variant + R:R combo we use should round-trip cleanly."""
    cases = [
        ("ema_cross_9_20", {"fast_period": 9, "slow_period": 20,
                              "stop_atr_mult": 1.5, "target_atr_mult": 3.0}),
        ("donchian_55", {"period": 55, "atr_period": 14,
                          "stop_atr_mult": 1.5, "target_atr_mult": 1.65}),
        ("rsi_30_70", {"oversold": 30, "overbought": 70,
                        "stop_atr_mult": 1.5, "target_atr_mult": 1.5}),
        ("bbands_20_2", {"bb_period": 20, "bb_k": 2.0}),
    ]
    for variant, cfg in cases:
        edge = EdgeStat(
            ticker="X", tf="H1", strategy=variant,
            n_train=10, train_pf=1.0, train_r=0.0,
            n_test=10, test_pf=1.0, test_r=0.0,
            source_config_json=json.dumps(cfg),
        )
        url = build_backtest_url(edge)
        blob = url.split("config=")[1]
        pad = "=" * (-len(blob) % 4)
        decoded = json.loads(
            base64.urlsafe_b64decode((blob + pad).encode("ascii"))
                  .decode("utf-8")
        )
        assert decoded == cfg, f"round-trip failed for {variant}"
