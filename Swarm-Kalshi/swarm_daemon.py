#!/usr/bin/env python3
"""
swarm_daemon.py
===============

24/7 watchdog daemon for the Kalshi trading swarm.

Features
--------
* Never gives up — unlimited restarts with exponential back-off (cap 10 min).
* Resets back-off counter when the child process runs healthily for ≥ 30 min.
* Dual logging: stdout + logs/daemon.log (rotating, 10 MB × 5 files).
* Graceful SIGINT / SIGTERM passthrough to the child process.
* Targets run_swarm_with_ollama_brain.py (LLM brain mode).

Usage
-----
  python swarm_daemon.py          # run interactively
  pythonw swarm_daemon.py         # run detached (no console window)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from swarm.meta_evolver import MetaEvolverAgent

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
SWARM_SCRIPT = str(PROJECT_ROOT / "run_swarm_with_ollama_brain.py")

BACKOFF_BASE      = 30      # seconds — first retry delay
BACKOFF_FACTOR    = 2       # doubles each failure
BACKOFF_CAP       = 600     # 10 minutes — maximum wait between retries
HEALTHY_RUN_SECS  = 1800    # 30 min — reset back-off if ran this long without crash

LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "daemon.log"

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _build_logger() -> logging.Logger:
    log = logging.getLogger("swarm_daemon")
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | DAEMON | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    log.addHandler(sh)

    # Rotating file
    fh = logging.handlers.RotatingFileHandler(
        str(LOG_FILE), maxBytes=10_485_760, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)

    return log


logger = _build_logger()

# ---------------------------------------------------------------------------
# Child-process management
# ---------------------------------------------------------------------------

_child: subprocess.Popen | None = None
_stop_requested = False
_daemon_meta_evolver: MetaEvolverAgent | None = None
_last_sunday_evolver_run_date: str = ""


def _signal_handler(signum, frame):
    global _stop_requested
    logger.info("Received signal %d — requesting clean stop.", signum)
    _stop_requested = True
    if _child and _child.poll() is None:
        logger.info("Forwarding signal to child PID %d.", _child.pid)
        try:
            _child.terminate()
        except Exception as exc:
            logger.warning("Failed to terminate child process: %s", exc)


signal.signal(signal.SIGINT,  _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _run_once() -> int:
    """Spawn the swarm process, stream its output, return its exit code."""
    global _child

    python = sys.executable
    cmd = [python, SWARM_SCRIPT]
    env = {**os.environ}

    logger.info("Launching: %s", " ".join(cmd))

    _child = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    logger.info("Child PID: %d", _child.pid)

    # Stream child stdout to our logger
    for line in _child.stdout:
        line = line.rstrip()
        if line:
            logger.info("[SWARM] %s", line)
            _maybe_run_sunday_meta_evolver(line)

    _child.wait()
    code = _child.returncode
    _child = None
    return code


# ---------------------------------------------------------------------------


def _build_agent_configs_for_evolver() -> dict:
    """
    Build in-memory agent config view for MetaEvolver from bot YAML files.
    """
    agent_configs: dict = {}
    bot_names = ("sentinel", "oracle", "pulse", "vanguard")

    for bot_name in bot_names:
        bot_cfg_path = PROJECT_ROOT / "config" / f"{bot_name}_config.yaml"
        if not bot_cfg_path.exists():
            continue
        try:
            with open(bot_cfg_path, "r", encoding="utf-8") as fh:
                bot_cfg = yaml.safe_load(fh) or {}
            trading = bot_cfg.get("trading", {}) or {}
            llm_cfg = bot_cfg.get("llm_advisor", {}) or {}
            agent_configs[bot_name] = {
                "temperature": float(llm_cfg.get("temperature", 0.1) or 0.1),
                "confidence_threshold": float(
                    trading.get("min_confidence_threshold", 65) or 65
                ),
                "max_signals_per_cycle": int(
                    trading.get("max_signals_per_cycle", 3) or 3
                ),
            }
        except Exception as exc:
            logger.warning("Failed to load bot config for MetaEvolver (%s): %s", bot_name, exc)

    return agent_configs

def _build_meta_evolver_from_config() -> MetaEvolverAgent | None:
    cfg_path = PROJECT_ROOT / "config" / "swarm_config.yaml"
    if not cfg_path.exists():
        return None
    try:
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        meta_cfg = cfg.get("meta_evolver", {}) or {}
        if not bool(meta_cfg.get("enabled", False)):
            return None
        return MetaEvolverAgent(config=meta_cfg, project_root=str(PROJECT_ROOT))
    except Exception as exc:
        logger.warning("MetaEvolver daemon init failed: %s", exc)
        return None


def _maybe_run_sunday_meta_evolver(sw_line: str) -> None:
    """
    Trigger MetaEvolver Sunday job after weekly recalibration log line.
    """
    global _daemon_meta_evolver, _last_sunday_evolver_run_date

    if "Weekly backtest result:" not in sw_line:
        return

    now = datetime.utcnow()
    if now.weekday() != 6:  # Sunday
        return

    run_date = now.date().isoformat()
    if _last_sunday_evolver_run_date == run_date:
        return

    if _daemon_meta_evolver is None:
        _daemon_meta_evolver = _build_meta_evolver_from_config()
    if _daemon_meta_evolver is None:
        return

    try:
        result = _daemon_meta_evolver.execute(
            {
                "trigger": "sunday_post_rl_recalibration",
                "agent_configs": _build_agent_configs_for_evolver(),
                "source": "swarm_daemon",
            }
        )
        logger.info("Sunday MetaEvolver executed: %s", result)
        _last_sunday_evolver_run_date = run_date
    except Exception as exc:
        logger.warning("Sunday MetaEvolver execution failed: %s", exc)

# Main daemon loop
# ---------------------------------------------------------------------------

def main() -> int:
    logger.info("=" * 60)
    logger.info("Kalshi Swarm Daemon — 24/7 mode")
    logger.info("Script : %s", SWARM_SCRIPT)
    logger.info("Log    : %s", LOG_FILE)
    logger.info("=" * 60)

    restart_count = 0
    backoff = BACKOFF_BASE

    while not _stop_requested:
        start_ts = time.monotonic()

        exit_code = _run_once()

        elapsed = time.monotonic() - start_ts

        if _stop_requested:
            logger.info("Stop requested — daemon exiting cleanly.")
            break

        # Clean exits (0 = intentional shutdown)
        if exit_code == 0:
            logger.info("Swarm exited cleanly (code 0). Daemon stopping.")
            break

        restart_count += 1
        logger.warning(
            "Swarm exited with code %d after %.0f s (restart #%d).",
            exit_code, elapsed, restart_count,
        )

        # Reset back-off if the process was healthy for a long run
        if elapsed >= HEALTHY_RUN_SECS:
            logger.info(
                "Process ran %.0f s — resetting back-off to %d s.",
                elapsed, BACKOFF_BASE,
            )
            backoff = BACKOFF_BASE
        else:
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_CAP)

        logger.info("Waiting %d s before restart...", backoff)

        # Sleep in small intervals so we can react to stop signals
        deadline = time.monotonic() + backoff
        while time.monotonic() < deadline and not _stop_requested:
            time.sleep(1)

    logger.info("Daemon finished. Total restarts: %d.", restart_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())

