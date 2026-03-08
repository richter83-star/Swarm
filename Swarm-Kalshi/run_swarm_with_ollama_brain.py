#!/usr/bin/env python3
"""
run_swarm_with_ollama_brain.py
==============================

Starts the Kalshi swarm with centralized Ollama-based trade approvals enabled.
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Tuple

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kalshi_swarm_ollama")


def _load_ollama_config() -> Tuple[str, str]:
    """Load central_llm endpoint/model from swarm config with safe defaults."""
    config_path = PROJECT_ROOT / "config" / "swarm_config.yaml"
    defaults = ("http://127.0.0.1:11434", "qwen2.5:14b")

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
    base_url = str(central.get("ollama_base_url", defaults[0]))
    model = str(central.get("model", defaults[1]))
    return base_url, model


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


def main() -> int:
    print("\n" + "=" * 72)
    print("  KALSHI BOT SWARM - CENTRAL OLLAMA BRAIN MODE")
    print("=" * 72)

    ollama_url, model = _load_ollama_config()

    if not _check_ollama(ollama_url, model):
        print("\n[ERROR] Ollama preflight failed. Start Ollama or pull the configured model.")
        return 1

    print("[OK] Ollama reachable and model available.")
    print("[INFO] Launching swarm with centralized LLM approvals enabled...\n")

    from run_swarm import main as run_swarm_main

    run_swarm_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
