#!/usr/bin/env python3
"""
run_swarm_with_ollama_brain.py
==============================

Starts the Kalshi swarm with centralized LLM trade approvals enabled.
Provider is selected from config: central_llm.provider (anthropic or ollama).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kalshi_swarm_ollama")


def _load_central_llm_config() -> Dict[str, Any]:
    """Load central_llm config from swarm config with safe defaults."""
    config_path = PROJECT_ROOT / "config" / "swarm_config.yaml"
    defaults: Dict[str, Any] = {
        "provider": "anthropic",
        "ollama_base_url": "http://127.0.0.1:11434",
        "model": "claude-3-5-haiku-latest",
        "anthropic_model": "claude-3-5-haiku-latest",
        "anthropic_api_key": "",
    }

    if not config_path.exists():
        logger.warning("Swarm config not found at %s. Using defaults.", config_path)
        return defaults

    try:
        with config_path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("Failed to read %s: %s. Using defaults.", config_path, exc)
        return defaults

    central = cfg.get("central_llm", {}) if isinstance(cfg, dict) else {}
    if not isinstance(central, dict):
        return defaults
    return {**defaults, **central}


def _check_ollama(base_url: str, model: str, timeout: int = 5) -> bool:
    url = f"{base_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        logger.error("Ollama is not reachable at %s: %s", url, exc)
        return False

    models = body.get("models", []) if isinstance(body, dict) else []
    names = {m.get("name", "") for m in models if isinstance(m, dict)}
    if model not in names:
        logger.warning("Configured model '%s' not found in local Ollama tags.", model)
        logger.warning("Available models: %s", ", ".join(sorted(names)) or "(none)")
        return False
    return True


def _check_anthropic(config: Dict[str, Any]) -> bool:
    """Anthropic preflight: verify API key is available."""
    key = str(config.get("anthropic_api_key", "")).strip() or str(
        os.environ.get("ANTHROPIC_API_KEY", "")
    ).strip()
    if not key:
        logger.error(
            "Anthropic provider selected but no API key found "
            "(set central_llm.anthropic_api_key or ANTHROPIC_API_KEY)."
        )
        return False
    return True


def main() -> int:
    print("\n" + "=" * 72)
    print("  KALSHI BOT SWARM - CENTRAL LLM BRAIN MODE")
    print("=" * 72)

    central_cfg = _load_central_llm_config()
    provider = str(central_cfg.get("provider", "anthropic")).strip().lower()

    if provider in {"anthropic", "claude"}:
        model = str(central_cfg.get("anthropic_model") or central_cfg.get("model") or "claude-3-5-haiku-latest")
        if not _check_anthropic(central_cfg):
            print("\n[ERROR] Anthropic preflight failed. Configure API key first.")
            return 1
        print(f"[OK] Anthropic provider configured (model={model}).")
    else:
        ollama_url = str(central_cfg.get("ollama_base_url", "http://127.0.0.1:11434"))
        model = str(central_cfg.get("model", "qwen2.5:14b"))
        if not _check_ollama(ollama_url, model):
            print("\n[ERROR] Ollama preflight failed. Start Ollama or pull the configured model.")
            return 1
        print(f"[OK] Ollama reachable and model available ({model}).")

    print("[INFO] Launching swarm with centralized LLM approvals enabled...\n")

    from run_swarm import main as run_swarm_main

    run_swarm_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
