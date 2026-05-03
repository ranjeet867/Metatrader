"""
test_config.py — risk_config.json loader.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core import config


def _tmp_path() -> Path:
    return Path(tempfile.mkstemp(suffix=".json")[1])


class TestDefaults:
    def test_load_creates_file_with_defaults_when_missing(self):
        # Use a fresh path that does NOT exist yet
        d = Path(tempfile.mkdtemp())
        path = d / "risk_config.json"
        assert not path.exists()
        cfg = config.load_config(path)
        assert path.exists()
        # And the loaded config matches DEFAULT_CONFIG values
        assert cfg.weekend_flat_all is True
        assert "stock" in cfg.daily_close_flat_classes
        assert "index" in cfg.daily_close_flat_classes
        assert cfg.daily_loss_cap_pct == 4.5
        assert cfg.max_consecutive_losses == 3
        assert "US100.cash" in cfg.asset_class_overrides["index"]

    def test_load_is_idempotent_does_not_overwrite_existing(self):
        path = _tmp_path()
        # Pre-write a CUSTOM config
        custom = config.default_config_dict()
        custom["weekend_flat_all"] = False
        custom["daily_loss_cap_pct"] = 3.0
        path.write_text(json.dumps(custom))
        # Now load — must NOT regenerate defaults
        cfg = config.load_config(path)
        assert cfg.weekend_flat_all is False
        assert cfg.daily_loss_cap_pct == 3.0


class TestValidation:
    def test_missing_required_key_raises(self):
        path = _tmp_path()
        broken = config.default_config_dict()
        broken.pop("daily_loss_cap_pct")
        path.write_text(json.dumps(broken))
        with pytest.raises(ValueError, match="missing required keys"):
            config.load_config(path)

    def test_invalid_json_raises(self):
        path = _tmp_path()
        path.write_text("{not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            config.load_config(path)

    def test_top_level_must_be_object(self):
        path = _tmp_path()
        path.write_text("[]")
        with pytest.raises(ValueError, match="must be a JSON object"):
            config.load_config(path)

    def test_save_round_trips(self):
        path = _tmp_path()
        custom = config.default_config_dict()
        custom["max_consecutive_losses"] = 5
        config.save_config(custom, path)
        cfg = config.load_config(path)
        assert cfg.max_consecutive_losses == 5

    def test_save_with_invalid_value_rejects(self):
        path = _tmp_path()
        bad = config.default_config_dict()
        del bad["weekend_flat_all"]
        with pytest.raises(ValueError):
            config.save_config(bad, path)

    def test_overrides_normalized_to_tuples(self):
        path = _tmp_path()
        cfg_dict = config.default_config_dict()
        path.write_text(json.dumps(cfg_dict))
        cfg = config.load_config(path)
        # Tuples are immutable & frozen-dataclass-friendly
        assert isinstance(cfg.asset_class_overrides["index"], tuple)
        assert isinstance(cfg.daily_close_flat_classes, tuple)
