"""pytest configuration for mt5_quant_trader_v2 tests.

Disable the LiveExecutor parity-fallback to the main repo DB for all
tests by default. Production runner enables it via the env-var default;
tests need isolation to make their own assertions about parity recency
without being affected by parity rows that happen to live in the
checked-in dev DB.

Tests that explicitly want to validate the fallback path should set
MT5QT_PARITY_FALLBACK_REPO=1 inside the test (or clear the env var).
"""
from __future__ import annotations

import os


def pytest_configure(config):
    # Default-off in tests; production keeps the fallback active.
    os.environ.setdefault("MT5QT_PARITY_FALLBACK_REPO", "0")
