"""
config_validator.py
===================

Validates swarm_config.yaml at startup.  Raises ``ConfigValidationError``
on fatal problems so the swarm fails fast with a clear message rather than
misbehaving silently at runtime.

Usage
-----
    from swarm.config_validator import validate_config
    validate_config(cfg, project_root=PROJECT_ROOT)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """Raised when the configuration contains a fatal error."""


def validate_config(cfg: Dict[str, Any], project_root: Path | None = None) -> None:
    """
    Validate a loaded swarm config dict.

    Parameters
    ----------
    cfg:
        The dict returned by ``yaml.safe_load(swarm_config.yaml)``.
    project_root:
        Project root directory used to resolve relative paths.

    Raises
    ------
    ConfigValidationError
        On any fatal misconfiguration.  Logs warnings for non-fatal issues.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(cfg, dict):
        raise ConfigValidationError("Config file is empty or not a valid YAML mapping.")

    # ------------------------------------------------------------------
    # [api] section — required
    # ------------------------------------------------------------------
    api = cfg.get("api")
    if not isinstance(api, dict):
        errors.append("Missing required section: 'api'")
    else:
        key_id = str(api.get("key_id", "")).strip()
        if not key_id or key_id == "YOUR_API_KEY_ID_HERE":
            errors.append(
                "api.key_id is not set. Add your Kalshi API key ID to swarm_config.yaml "
                "or set the KALSHI_KEY_ID environment variable."
            )

        raw_key_path = str(api.get("private_key_path", "")).strip()
        if not raw_key_path:
            errors.append("api.private_key_path is not set.")
        else:
            key_path = Path(raw_key_path)
            if not key_path.is_absolute() and project_root is not None:
                key_path = Path(project_root) / key_path
            if not key_path.exists():
                errors.append(
                    f"api.private_key_path '{key_path}' does not exist. "
                    "Use an absolute path or ensure the file is present relative to the project root."
                )

        base_url = str(api.get("base_url", "")).strip()
        if not base_url:
            errors.append("api.base_url is not set.")
        elif not base_url.startswith("https://"):
            warnings.append(f"api.base_url does not use HTTPS: '{base_url}'")

    # ------------------------------------------------------------------
    # [central_llm] section — validate Anthropic key when provider=anthropic
    # ------------------------------------------------------------------
    central_llm = cfg.get("central_llm")
    if isinstance(central_llm, dict) and bool(central_llm.get("enabled", True)):
        provider = str(central_llm.get("provider", "anthropic")).strip().lower()
        if provider in {"anthropic", "claude"}:
            api_key = str(central_llm.get("anthropic_api_key", "")).strip() or str(
                os.environ.get("ANTHROPIC_API_KEY", "")
            ).strip()
            if not api_key:
                errors.append(
                    "central_llm.provider is 'anthropic' but no API key found. "
                    "Set central_llm.anthropic_api_key in config or the ANTHROPIC_API_KEY env var."
                )

    # ------------------------------------------------------------------
    # [swarm] section — key limits
    # ------------------------------------------------------------------
    swarm = cfg.get("swarm")
    if not isinstance(swarm, dict):
        warnings.append("Missing 'swarm' section; using defaults.")
    else:
        loss_limit = swarm.get("global_daily_loss_limit_cents")
        if loss_limit is not None and (not isinstance(loss_limit, (int, float)) or loss_limit <= 0):
            errors.append("swarm.global_daily_loss_limit_cents must be a positive number.")

        exposure_limit = swarm.get("global_exposure_limit_cents")
        if exposure_limit is not None and (not isinstance(exposure_limit, (int, float)) or exposure_limit <= 0):
            errors.append("swarm.global_exposure_limit_cents must be a positive number.")

    # ------------------------------------------------------------------
    # [trading] section — sanity checks
    # ------------------------------------------------------------------
    trading = cfg.get("trading")
    if isinstance(trading, dict):
        conf_threshold = trading.get("min_confidence_threshold")
        if conf_threshold is not None:
            if not isinstance(conf_threshold, (int, float)) or not (0 < conf_threshold <= 100):
                errors.append("trading.min_confidence_threshold must be between 1 and 100.")

        max_pos_pct = trading.get("max_position_pct")
        if max_pos_pct is not None:
            if not isinstance(max_pos_pct, (int, float)) or not (0 < max_pos_pct <= 1.0):
                errors.append("trading.max_position_pct must be between 0 and 1.0.")

        min_balance = trading.get("min_balance_cents")
        if min_balance is not None and (not isinstance(min_balance, (int, float)) or min_balance < 0):
            errors.append("trading.min_balance_cents must be non-negative.")

    # ------------------------------------------------------------------
    # [dashboard] — security warnings
    # ------------------------------------------------------------------
    dashboard = cfg.get("dashboard")
    if isinstance(dashboard, dict):
        auth = dashboard.get("auth", {}) or {}
        if not bool(auth.get("enabled", False)):
            warnings.append(
                "dashboard.auth.enabled is false — the dashboard is publicly accessible. "
                "Enable authentication before exposing it to any network."
            )
        elif not str(auth.get("password", "")).strip() and not os.environ.get("DASHBOARD_PASS"):
            warnings.append(
                "dashboard.auth.enabled is true but no password is set. "
                "Set dashboard.auth.password or the DASHBOARD_PASS env var."
            )

    # ------------------------------------------------------------------
    # Emit warnings and raise on errors
    # ------------------------------------------------------------------
    for w in warnings:
        logger.warning("[CONFIG] %s", w)

    if errors:
        bullet_list = "\n  - ".join(errors)
        raise ConfigValidationError(
            f"Config validation failed with {len(errors)} error(s):\n  - {bullet_list}"
        )

    logger.info("[CONFIG] Validation passed (%d warning(s)).", len(warnings))
