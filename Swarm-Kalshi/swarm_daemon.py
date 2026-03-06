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
* Targets run_swarm_with_openclaw_brain.py (LLM brain mode).

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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
SWARM_SCRIPT = str(PROJECT_ROOT / "run_swarm_with_openclaw_brain.py")

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

    _child.wait()
    code = _child.returncode
    _child = None
    return code


# ---------------------------------------------------------------------------
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
