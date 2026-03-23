#!/usr/bin/env python3
"""
run_swarm.py
============

Main entry point for the Kalshi Bot Swarm system.

Usage
-----
    # Start the full swarm (coordinator + all bots)
    python run_swarm.py

    # Start a single bot
    python run_swarm.py --bot sentinel

Dashboard runs separately:
    bash start_dashboard.sh   (port 8888)
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys

# Load .env file if present (must happen before any config is read)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed; rely on environment variables
import time
from pathlib import Path

# Ensure project root is on the path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("kalshi_swarm")


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging."""
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def ensure_directories() -> None:
    """Create required directories if they don't exist."""
    for d in ["data", "logs", "keys"]:
        (PROJECT_ROOT / d).mkdir(parents=True, exist_ok=True)


def validate_config_or_exit() -> None:
    """Load and validate swarm_config.yaml; exit on fatal errors."""
    import yaml
    from swarm.config_validator import validate_config, ConfigValidationError

    config_path = PROJECT_ROOT / "config" / "swarm_config.yaml"
    if not config_path.exists():
        logger.error("Config file not found: %s", config_path)
        raise SystemExit(1)

    with config_path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    try:
        validate_config(cfg, project_root=PROJECT_ROOT)
    except ConfigValidationError as exc:
        logger.error("Startup aborted:\n%s", exc)
        raise SystemExit(1)



def run_single_bot(bot_name: str) -> None:
    """Run a single bot directly (no coordinator)."""
    from swarm.bot_runner import BotRunner

    config_map = {
        "sentinel": "config/sentinel_config.yaml",
        "oracle": "config/oracle_config.yaml",
        "pulse": "config/pulse_config.yaml",
        "vanguard": "config/vanguard_config.yaml",
    }

    if bot_name not in config_map:
        logger.error("Unknown bot: %s. Choose from: %s", bot_name, list(config_map.keys()))
        sys.exit(1)

    runner = BotRunner(
        bot_name=bot_name,
        swarm_config_path=str(PROJECT_ROOT / "config" / "swarm_config.yaml"),
        bot_config_path=str(PROJECT_ROOT / config_map[bot_name]),
        project_root=str(PROJECT_ROOT),
    )
    runner.run()


def run_full_swarm() -> None:
    """Run the full swarm: coordinator + all bots."""
    from swarm.swarm_coordinator import SwarmCoordinator

    coordinator = SwarmCoordinator(
        config_path="config/swarm_config.yaml",
        project_root=str(PROJECT_ROOT),
    )
    # Run coordinator (blocks until shutdown)
    coordinator.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kalshi Bot Swarm -- Main Entry Point",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_swarm.py                # Full swarm (all 4 bots)
  python run_swarm.py --bot sentinel # Single bot
  bash start_dashboard.sh            # Dashboard on port 8888 (separate)
        """,
    )
    parser.add_argument(
        "--bot",
        choices=["sentinel", "oracle", "pulse", "vanguard"],
        help="Run a single bot instead of the full swarm.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    ensure_directories()
    validate_config_or_exit()

    print(r"""
    ╔══════════════════════════════════════════════════════════╗
    ║           🐝  KALSHI BOT SWARM  v3.0.0  🐝             ║
    ║                                                          ║
    ║   Sentinel  ·  Oracle  ·  Pulse  ·  Vanguard            ║
    ║                                                          ║
    ╚══════════════════════════════════════════════════════════╝
    """)

    if args.bot:
        run_single_bot(args.bot)
    else:
        run_full_swarm()


if __name__ == "__main__":
    main()
