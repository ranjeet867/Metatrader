"""
test_data.py — verify load/save parquet round-trips and validation paths.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from core.data import load_parquet, save_parquet
from core.storage import validate_candles
from tests.fixtures.synthetic import linear_ramp


def _tmp(suffix=".parquet") -> Path:
    return Path(tempfile.mkstemp(suffix=suffix)[1])


class TestParquetRoundTrip:
    def test_save_then_load_byte_identical(self):
        df = linear_ramp(start_price=100, step=0.5, n_bars=100)
        p = _tmp()
        save_parquet(df, p)
        loaded = load_parquet(p)
        # Same length, same values
        assert len(loaded) == len(df)
        for col in ("open", "high", "low", "close", "volume"):
            assert (loaded[col].values == df[col].values).all(), \
                f"col {col} differs after round-trip"
        # Times match
        assert (loaded["time"].values == df["time"].values).all()

    def test_save_is_idempotent(self):
        """Saving the same df twice produces identical bytes."""
        df = linear_ramp(n_bars=50)
        p1 = _tmp()
        p2 = _tmp()
        save_parquet(df, p1)
        save_parquet(df, p2)
        # Files should be byte-identical
        assert p1.read_bytes() == p2.read_bytes()

    def test_save_rejects_invalid_candles(self):
        """A df with high<low must be rejected at save time, not silently saved."""
        bad = pd.DataFrame({
            "time": [pd.Timestamp("2024-01-01", tz="UTC")],
            "open": [100.0], "high": [99.0], "low": [101.0], "close": [100.0],
            "volume": [1.0],
        })
        with pytest.raises(ValueError):
            save_parquet(bad, _tmp())


class TestValidationOnLoad:
    def test_load_validates_existing_file(self):
        """If a parquet on disk has bad data, load_parquet raises."""
        # Write bad data manually (bypass our validator)
        bad = pd.DataFrame({
            "time": pd.to_datetime(["2024-01-01"], utc=True),
            "open": [100.0], "high": [-5.0], "low": [99.0], "close": [100.0],
            "volume": [1.0],
        })
        p = _tmp()
        bad.to_parquet(p, index=False)
        with pytest.raises(ValueError):
            load_parquet(p)
