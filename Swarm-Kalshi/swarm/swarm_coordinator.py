"""
swarm_coordinator.py
====================

Central coordinator that manages all 4 specialist bots as a unified
swarm.

Responsibilities
----------------
1. Spawn, stop, and restart bot processes.
2. Coordinate shared account balance (allocate budget per bot).
3. Prevent position conflicts (no two bots on the same ticker).
4. Enforce global risk limits (total daily loss, total exposure).
5. Stagger bot activity for human-like API patterns.
6. Route markets to the correct specialist bot.
7. Health monitoring and automatic restarts.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from swarm.balance_manager import BalanceManager
from swarm.conflict_resolver import ConflictResolver
from swarm.market_router import MarketRouter

logger = logging.getLogger("swarm_coordinator")


# Bot definitions
BOT_DEFINITIONS = {
    "sentinel": {
        "display_name": "Sentinel",
        "config_file": "config/sentinel_config.yaml",
        "specialist": "politics",
    },
    "oracle": {
        "display_name": "Oracle",
        "config_file": "config/oracle_config.yaml",
        "specialist": "economics",
    },
    "pulse": {
        "display_name": "Pulse",
        "config_file": "config/pulse_config.yaml",
        "specialist": "weather",
    },
    "vanguard": {
        "display_name": "Vanguard",
        "config_file": "config/vanguard_config.yaml",
        "specialist": "general",
    },
}


class BotProcess:
    """Tracks a running bot subprocess."""

    def __init__(self, name: str, definition: Dict[str, Any]):
        self.name = name
        self.definition = definition
        self.process: Optional[subprocess.Popen] = None
        self.restart_count: int = 0
        self.last_restart: Optional[datetime] = None
        self.started_at: Optional[datetime] = None
        self.state: str = "stopped"
        self.error: str = ""


class SwarmCoordinator:
    """
    Central coordinator for the Kalshi bot swarm.

    Parameters
    ----------
    config_path : str
        Path to ``swarm_config.yaml``.
    project_root : str
        Root directory of the project.
    """

    def __init__(
        self,
        config_path: str = "config/swarm_config.yaml",
        project_root: str = ".",
    ):
        self.project_root = Path(project_root).resolve()
        self.config_path = self.project_root / config_path

        with open(self.config_path) as fh:
            self.cfg = yaml.safe_load(fh)

        self._setup_logging()
        self._running = True

        # Swarm config
        self.swarm_cfg = self.cfg.get("swarm", {})

        # Initialize subsystems
        self.balance_manager = BalanceManager(self.swarm_cfg)
        self.conflict_resolver = ConflictResolver()

        # Load bot configs for router
        bot_configs = {}
        for bot_name, bot_def in BOT_DEFINITIONS.items():
            cfg_path = self.project_root / bot_def["config_file"]
            if cfg_path.exists():
                with open(cfg_path) as fh:
                    bot_configs[bot_name] = yaml.safe_load(fh) or {}

        self.market_router = MarketRouter(
            bot_configs,
            default_bot=self.swarm_cfg.get("unassigned_market_bot", "vanguard"),
        )

        # Bot processes
        self.bots: Dict[str, BotProcess] = {}
        for name, definition in BOT_DEFINITIONS.items():
            self.bots[name] = BotProcess(name, definition)

        # Activity log for dashboard
        self._activity_log: List[Dict[str, Any]] = []
        self._activity_lock = threading.Lock()
        self._max_activity_log = 200

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        log_cfg = self.cfg.get("logging", {})
        level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | SWARM | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

        log_path = self.project_root / "logs" / "swarm_coordinator.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=log_cfg.get("max_bytes", 10_485_760),
            backupCount=log_cfg.get("backup_count", 5),
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
        logger.setLevel(level)

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %d -- shutting down swarm.", signum)
        self._running = False

    # ------------------------------------------------------------------
    # Bot process management
    # ------------------------------------------------------------------

    def start_bot(self, bot_name: str) -> bool:
        """Start a single bot as a subprocess."""
        if bot_name not in self.bots:
            logger.error("Unknown bot: %s", bot_name)
            return False

        bot = self.bots[bot_name]
        if bot.process and bot.process.poll() is None:
            logger.warning("Bot %s is already running (PID %d).", bot_name, bot.process.pid)
            return False

        config_file = bot.definition["config_file"]
        config_path = self.project_root / config_file

        if not config_path.exists():
            logger.error("Config file not found: %s", config_path)
            return False

        # Stagger startup
        delay = self._get_stagger_delay()
        if delay > 0:
            logger.info("Staggering %s startup by %.1f seconds.", bot_name, delay)
            time.sleep(delay)

        cmd = [
            sys.executable, "-m", "swarm.bot_runner",
            "--bot-name", bot_name,
            "--swarm-config", str(self.config_path),
            "--bot-config", str(config_path),
            "--project-root", str(self.project_root),
        ]

        try:
            bot.process = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            bot.started_at = datetime.now(timezone.utc)
            bot.state = "running"
            bot.error = ""

            logger.info(
                "Started bot %s (PID %d).",
                bot_name, bot.process.pid,
            )
            self._log_activity(bot_name, "started", f"PID {bot.process.pid}")
            return True

        except Exception as exc:
            logger.error("Failed to start bot %s: %s", bot_name, exc)
            bot.state = "error"
            bot.error = str(exc)
            return False

    def stop_bot(self, bot_name: str) -> bool:
        """Stop a running bot."""
        if bot_name not in self.bots:
            return False

        bot = self.bots[bot_name]
        if not bot.process or bot.process.poll() is not None:
            bot.state = "stopped"
            return True

        try:
            bot.process.terminate()
            try:
                bot.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                bot.process.kill()
                bot.process.wait(timeout=5)

            bot.state = "stopped"
            logger.info("Stopped bot %s.", bot_name)
            self._log_activity(bot_name, "stopped", "")
            return True

        except Exception as exc:
            logger.error("Failed to stop bot %s: %s", bot_name, exc)
            bot.state = "error"
            bot.error = str(exc)
            return False

    def restart_bot(self, bot_name: str) -> bool:
        """Restart a bot."""
        self.stop_bot(bot_name)
        time.sleep(2)
        bot = self.bots[bot_name]
        bot.restart_count += 1
        bot.last_restart = datetime.now(timezone.utc)
        self._log_activity(bot_name, "restarting", f"Restart #{bot.restart_count}")
        return self.start_bot(bot_name)

    def start_all(self) -> None:
        """Start all bots with staggered timing."""
        logger.info("Starting all bots...")
        for bot_name in self.bots:
            self.start_bot(bot_name)

    def stop_all(self) -> None:
        """Stop all running bots."""
        logger.info("Stopping all bots...")
        for bot_name in self.bots:
            self.stop_bot(bot_name)

    def pause_bot(self, bot_name: str) -> bool:
        """Pause a bot (write a pause signal file)."""
        if bot_name not in self.bots:
            return False
        signal_file = self.project_root / "data" / f"{bot_name}_signal.json"
        with open(signal_file, "w") as fh:
            json.dump({"command": "pause"}, fh)
        self.bots[bot_name].state = "paused"
        self._log_activity(bot_name, "paused", "")
        return True

    def resume_bot(self, bot_name: str) -> bool:
        """Resume a paused bot."""
        if bot_name not in self.bots:
            return False
        signal_file = self.project_root / "data" / f"{bot_name}_signal.json"
        with open(signal_file, "w") as fh:
            json.dump({"command": "resume"}, fh)
        self.bots[bot_name].state = "running"
        self._log_activity(bot_name, "resumed", "")
        return True

    def pause_all(self) -> None:
        """Pause all bots."""
        for bot_name in self.bots:
            self.pause_bot(bot_name)

    def resume_all(self) -> None:
        """Resume all bots."""
        for bot_name in self.bots:
            self.resume_bot(bot_name)

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    def check_health(self) -> Dict[str, str]:
        """Check health of all bots and restart crashed ones."""
        health = {}
        max_restarts = self.swarm_cfg.get("max_restart_attempts", 3)
        cooldown = self.swarm_cfg.get("restart_cooldown_seconds", 300)

        for bot_name, bot in self.bots.items():
            if bot.process is None:
                health[bot_name] = "not_started"
                continue

            poll = bot.process.poll()
            if poll is None:
                # Process is running -- check status file
                status = self._read_bot_status(bot_name)
                if status:
                    bot.state = status.get("state", "unknown")
                health[bot_name] = bot.state
            else:
                # Process has exited
                bot.state = "crashed"
                health[bot_name] = "crashed"
                logger.warning(
                    "Bot %s crashed (exit code %d). Restart count: %d/%d",
                    bot_name, poll, bot.restart_count, max_restarts,
                )

                # Auto-restart if within limits
                if bot.restart_count < max_restarts:
                    if bot.last_restart:
                        elapsed = (datetime.now(timezone.utc) - bot.last_restart).total_seconds()
                        if elapsed < cooldown:
                            logger.info(
                                "Restart cooldown for %s: %.0fs remaining.",
                                bot_name, cooldown - elapsed,
                            )
                            continue

                    self._log_activity(bot_name, "crashed", f"Exit code {poll}")
                    self.restart_bot(bot_name)
                    health[bot_name] = "restarting"
                else:
                    logger.error(
                        "Bot %s exceeded max restart attempts (%d). Manual intervention required.",
                        bot_name, max_restarts,
                    )
                    self._log_activity(
                        bot_name, "failed",
                        f"Exceeded {max_restarts} restart attempts",
                    )

        return health

    def _read_bot_status(self, bot_name: str) -> Optional[Dict]:
        """Read a bot's status file."""
        status_file = self.project_root / "data" / f"{bot_name}_status.json"
        try:
            if status_file.exists():
                with open(status_file) as fh:
                    return json.load(fh)
        except Exception as exc:
            logger.warning("Failed to read status file for %s: %s", bot_name, exc)
        return None

    # ------------------------------------------------------------------
    # Stagger timing
    # ------------------------------------------------------------------

    def _get_stagger_delay(self) -> float:
        """Get a random stagger delay for human-like behavior."""
        import random
        lo = self.swarm_cfg.get("min_inter_bot_delay_seconds", 5)
        hi = self.swarm_cfg.get("max_inter_bot_delay_seconds", 30)
        return random.uniform(lo, hi)

    # ------------------------------------------------------------------
    # Activity logging
    # ------------------------------------------------------------------

    def _log_activity(self, bot_name: str, action: str, detail: str) -> None:
        """Log an activity event for the dashboard."""
        with self._activity_lock:
            self._activity_log.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "bot": bot_name,
                "action": action,
                "detail": detail,
            })
            # Trim to max size
            if len(self._activity_log) > self._max_activity_log:
                self._activity_log = self._activity_log[-self._max_activity_log:]

    def get_activity_log(self, limit: int = 50) -> List[Dict]:
        """Return recent activity log entries."""
        with self._activity_lock:
            return list(reversed(self._activity_log[-limit:]))

    # ------------------------------------------------------------------
    # Status aggregation
    # ------------------------------------------------------------------

    def get_swarm_status(self) -> Dict[str, Any]:
        """Return comprehensive swarm status for the dashboard."""
        bot_statuses = {}
        for bot_name in self.bots:
            status = self._read_bot_status(bot_name)
            bot = self.bots[bot_name]
            if status:
                status["process_state"] = bot.state
                status["restart_count"] = bot.restart_count
                status["pid"] = bot.process.pid if bot.process and bot.process.poll() is None else None
            else:
                status = {
                    "bot_name": bot_name,
                    "display_name": bot.definition.get("display_name", bot_name),
                    "specialist": bot.definition.get("specialist", "unknown"),
                    "state": bot.state,
                    "process_state": bot.state,
                    "restart_count": bot.restart_count,
                    "pid": bot.process.pid if bot.process and bot.process.poll() is None else None,
                    "performance": {
                        "total_trades": 0, "wins": 0, "losses": 0,
                        "win_rate": 0.0, "total_pnl": 0, "avg_pnl": 0.0,
                    },
                    "risk": {
                        "balance_cents": 0, "daily_pnl_cents": 0,
                        "trades_today": 0, "wins_today": 0, "losses_today": 0,
                    },
                }
            bot_statuses[bot_name] = status

        # Aggregate metrics
        total_trades = sum(
            s.get("performance", {}).get("total_trades", 0)
            for s in bot_statuses.values()
        )
        total_wins = sum(
            s.get("performance", {}).get("wins", 0)
            for s in bot_statuses.values()
        )
        total_pnl = sum(
            s.get("performance", {}).get("total_pnl", 0)
            for s in bot_statuses.values()
        )

        return {
            "swarm_state": "running" if self._running else "stopped",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bots": bot_statuses,
            "global_metrics": {
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_losses": total_trades - total_wins,
                "overall_win_rate": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0,
                "total_pnl_cents": total_pnl,
                "total_pnl_dollars": round(total_pnl / 100, 2),
            },
            "balance": self.balance_manager.status(),
            "conflicts": self.conflict_resolver.status(),
            "activity": self.get_activity_log(limit=30),
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main coordinator loop."""
        logger.info("=" * 60)
        logger.info("KALSHI BOT SWARM COORDINATOR starting up.")
        logger.info("Bots: %s", ", ".join(self.bots.keys()))
        logger.info("=" * 60)

        self.start_all()

        check_interval = self.swarm_cfg.get("health_check_interval_seconds", 60)

        while self._running:
            try:
                self.check_health()
                self.conflict_resolver.prune_stale_claims()
                time.sleep(check_interval)
            except Exception as exc:
                logger.exception("Coordinator error: %s", exc)
                time.sleep(10)

        self._shutdown()

    def _shutdown(self) -> None:
        """Graceful shutdown of the entire swarm."""
        logger.info("Shutting down swarm...")
        self.stop_all()
        logger.info("Swarm coordinator stopped.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Bot Swarm Coordinator")
    parser.add_argument(
        "--config",
        default="config/swarm_config.yaml",
        help="Path to swarm config",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root directory",
    )
    args = parser.parse_args()

    coordinator = SwarmCoordinator(
        config_path=args.config,
        project_root=args.project_root,
    )
    coordinator.run()


if __name__ == "__main__":
    main()

