"""
tests/test_config_validator.py
================================

Unit tests for swarm.config_validator — validates that the validator
correctly accepts valid configs, rejects bad ones, and warns on
security issues.
"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.config_validator import validate_config, ConfigValidationError


def _valid_cfg(tmp_path: Path) -> dict:
    """Minimal valid config with a real key file on disk."""
    key_file = tmp_path / "keys" / "kalshi.key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\ndummy\n-----END RSA PRIVATE KEY-----\n")
    return {
        "api": {
            "key_id": "real-key-id-abc123",
            "private_key_path": str(key_file),
            "base_url": "https://api.elections.kalshi.com/trade-api/v2",
            "demo_mode": True,
        },
        "central_llm": {
            "enabled": True,
            "provider": "anthropic",
            "anthropic_api_key": "sk-ant-test",
        },
        "swarm": {
            "global_daily_loss_limit_cents": 15000,
            "global_exposure_limit_cents": 50000,
        },
        "trading": {
            "min_confidence_threshold": 65,
            "max_position_pct": 0.01,
            "min_balance_cents": 500,
        },
        "dashboard": {
            "auth": {"enabled": True, "password": "secret"},
        },
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_valid_config_passes(tmp_path):
    validate_config(_valid_cfg(tmp_path))  # must not raise


# ---------------------------------------------------------------------------
# api section errors
# ---------------------------------------------------------------------------

def test_missing_api_section_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    del cfg["api"]
    with pytest.raises(ConfigValidationError, match="Missing required section: 'api'"):
        validate_config(cfg)


def test_placeholder_key_id_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["api"]["key_id"] = "YOUR_API_KEY_ID_HERE"
    with pytest.raises(ConfigValidationError, match="api.key_id"):
        validate_config(cfg)


def test_empty_key_id_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["api"]["key_id"] = ""
    with pytest.raises(ConfigValidationError, match="api.key_id"):
        validate_config(cfg)


def test_missing_key_file_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["api"]["private_key_path"] = "/nonexistent/path/key.pem"
    with pytest.raises(ConfigValidationError, match="does not exist"):
        validate_config(cfg)


def test_empty_private_key_path_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["api"]["private_key_path"] = ""
    with pytest.raises(ConfigValidationError, match="private_key_path"):
        validate_config(cfg)


def test_missing_base_url_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["api"]["base_url"] = ""
    with pytest.raises(ConfigValidationError, match="base_url"):
        validate_config(cfg)


def test_relative_key_path_resolved_via_project_root(tmp_path):
    cfg = _valid_cfg(tmp_path)
    key_file = tmp_path / "keys" / "kalshi.key"
    cfg["api"]["private_key_path"] = "keys/kalshi.key"  # relative
    validate_config(cfg, project_root=tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# central_llm errors
# ---------------------------------------------------------------------------

def test_anthropic_provider_without_key_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _valid_cfg(tmp_path)
    cfg["central_llm"]["anthropic_api_key"] = ""
    with pytest.raises(ConfigValidationError, match="API key"):
        validate_config(cfg)


def test_anthropic_key_from_env_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-test")
    cfg = _valid_cfg(tmp_path)
    cfg["central_llm"]["anthropic_api_key"] = ""
    validate_config(cfg)  # must not raise


def test_disabled_central_llm_skips_key_check(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _valid_cfg(tmp_path)
    cfg["central_llm"]["enabled"] = False
    cfg["central_llm"]["anthropic_api_key"] = ""
    validate_config(cfg)  # must not raise


# ---------------------------------------------------------------------------
# trading section errors
# ---------------------------------------------------------------------------

def test_bad_confidence_threshold_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["trading"]["min_confidence_threshold"] = 150  # > 100
    with pytest.raises(ConfigValidationError, match="min_confidence_threshold"):
        validate_config(cfg)


def test_bad_max_position_pct_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["trading"]["max_position_pct"] = 2.0  # > 1.0
    with pytest.raises(ConfigValidationError, match="max_position_pct"):
        validate_config(cfg)


def test_negative_min_balance_raises(tmp_path):
    cfg = _valid_cfg(tmp_path)
    cfg["trading"]["min_balance_cents"] = -1
    with pytest.raises(ConfigValidationError, match="min_balance_cents"):
        validate_config(cfg)


# ---------------------------------------------------------------------------
# Non-fatal config (no raise, but logs warning)
# ---------------------------------------------------------------------------

def test_missing_swarm_section_does_not_raise(tmp_path):
    cfg = _valid_cfg(tmp_path)
    del cfg["swarm"]
    validate_config(cfg)  # warning only


def test_non_dict_input_raises():
    with pytest.raises(ConfigValidationError, match="not a valid YAML mapping"):
        validate_config(None)
