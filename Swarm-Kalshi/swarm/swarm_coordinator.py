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
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from kalshi_agent.kalshi_client import KalshiClient
from swarm.balance_manager import BalanceManager
from swarm.conflict_resolver import ConflictResolver
from swarm.market_router import MarketRouter
from telegram.notifier import TelegramNotifier
from telegram.bot import TelegramCommandBot

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
        self._status_mismatch_warned: set[str] = set()
        self.auto_scale_cfg = self.cfg.get("auto_scale", {})
        self._last_auto_scale_at: Optional[datetime] = None
        self._last_auto_scale_summary: Dict[str, Any] = {
            "enabled": bool(self.auto_scale_cfg.get("enabled", False)),
            "last_evaluated_at": None,
            "changes": [],
            "reason": "not_evaluated",
        }
        self._portfolio_client: Optional[KalshiClient] = None
        self._portfolio_counts_cache: Dict[str, Any] = {
            "timestamp": None,
            "counts": {
                "ui_open_markets": 0,
                "ui_pending_markets": 0,
                "api_open_legs": 0,
                "api_parent_markets": 0,
            },
        }
        self._portfolio_counts_ttl_seconds = int(
            self.swarm_cfg.get("portfolio_counts_ttl_seconds", 30)
        )
        self._market_status_cache: Dict[str, Dict[str, Any]] = {}
        self._market_status_ttl_seconds = int(
            self.swarm_cfg.get("market_status_cache_ttl_seconds", 120)
        )
        self._trade_guard_file = self.project_root / "data" / "swarm_trade_guard.json"
        self._trade_guard_file.parent.mkdir(parents=True, exist_ok=True)
        self._last_known_balance_cents: int = 0
        self._bot_learning_db_paths: Dict[str, Path] = self._resolve_bot_learning_db_paths()

        # Telegram integration
        tg_cfg = self.cfg.get("telegram", {})
        self.notifier = TelegramNotifier(tg_cfg)
        self.tg_bot = TelegramCommandBot(tg_cfg, coordinator=self, project_root=self.project_root)

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
                # Bot processes write to their own rotating log files.
                # Using PIPE without a reader can deadlock child processes.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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

    def _wait_for_bot_ready(self, bot_name: str, timeout: int = 600) -> bool:
        """Poll status file until bot reaches 'running' state (backtest done).

        PID-aware: only trusts a 'running' status if the PID recorded in the
        status file matches the newly-started process PID.  This prevents a
        stale status file from a prior crashed run from causing an immediate
        false-positive or an infinite 600-second wait on an old 'error' state.
        """
        deadline = time.monotonic() + timeout
        bot = self.bots.get(bot_name)
        expected_pid = bot.process.pid if (bot and bot.process) else None
        logger.info(
            "Waiting for %s (PID %s) to complete startup / backtest...",
            bot_name, expected_pid,
        )
        while time.monotonic() < deadline:
            status = self._read_bot_status(bot_name)
            if status:
                status_pid = status.get("pid")
                status_state = status.get("state")
                # Only trust this status entry if it was written by our process
                if expected_pid and status_pid != expected_pid:
                    logger.debug(
                        "%s: stale status file (pid=%s, expected=%s), waiting...",
                        bot_name, status_pid, expected_pid,
                    )
                elif status_state == "running":
                    logger.info("Bot %s (PID %s) is ready.", bot_name, expected_pid)
                    return True
            # Bail early if the process has already exited
            if bot and bot.process and bot.process.poll() is not None:
                logger.warning("Bot %s exited during startup.", bot_name)
                return False
            time.sleep(3)
        logger.warning("Timed out waiting for %s to become ready.", bot_name)
        return False

    def start_all(self) -> None:
        """Start all bots with staggered timing, waiting for each backtest to complete."""
        logger.info("Starting all bots...")
        for bot_name in self.bots:
            self.start_bot(bot_name)
            self._wait_for_bot_ready(bot_name)
        self.notifier.notify_swarm_started(list(self.bots.keys()))

    def stop_all(self) -> None:
        """Stop all running bots."""
        logger.info("Stopping all bots...")
        for bot_name in self.bots:
            self.stop_bot(bot_name)
        self.notifier.notify_swarm_stopped()

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
                if self._status_matches_process(bot, status):
                    bot.state = status.get("state", "unknown")
                    self._status_mismatch_warned.discard(bot_name)
                    health[bot_name] = bot.state
                else:
                    if bot_name not in self._status_mismatch_warned:
                        expected = bot.process.pid if bot.process else None
                        found = status.get("pid") if status else None
                        logger.warning(
                            "Ignoring stale status for %s (status pid=%s, process pid=%s).",
                            bot_name, found, expected,
                        )
                        self._status_mismatch_warned.add(bot_name)
                    bot.state = "running"
                    health[bot_name] = "running"
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
                    self.notifier.notify_crash(bot_name, poll)
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

    @staticmethod
    def _status_matches_process(bot: BotProcess, status: Optional[Dict[str, Any]]) -> bool:
        """Return True only when status PID matches the live subprocess PID."""
        if not status or not bot.process or bot.process.poll() is not None:
            return False
        try:
            return int(status.get("pid")) == int(bot.process.pid)
        except Exception:
            return False

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
    # Trade guard snapshot for bot pre-trade authorization
    # ------------------------------------------------------------------

    def _resolve_bot_learning_db_paths(self) -> Dict[str, Path]:
        """
        Resolve each bot's learning DB path from its bot config.
        """
        paths: Dict[str, Path] = {}
        for bot_name, bot_def in BOT_DEFINITIONS.items():
            cfg_path = self.project_root / bot_def["config_file"]
            db_path = self.project_root / "data" / f"{bot_name}.db"
            try:
                if cfg_path.exists():
                    with open(cfg_path, "r", encoding="utf-8") as fh:
                        cfg = yaml.safe_load(fh) or {}
                    learning = cfg.get("learning", {}) or {}
                    raw = str(learning.get("db_path", "")).strip()
                    if raw:
                        db_candidate = Path(raw)
                        if not db_candidate.is_absolute():
                            db_candidate = self.project_root / db_candidate
                        db_path = db_candidate
            except Exception as exc:
                logger.warning("Failed resolving learning DB path for %s: %s", bot_name, exc)
            paths[bot_name] = db_path
        return paths

    @staticmethod
    def _query_bot_trade_metrics(db_path: Path, today_utc: str) -> Dict[str, int]:
        """
        Return pending exposure and realized daily P&L from one bot DB.
        """
        if not db_path.exists():
            return {
                "pending_exposure_cents": 0,
                "pending_rows": 0,
                "daily_realized_pnl_cents": 0,
            }

        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            pending_row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(count * entry_price), 0) AS pending_exposure_cents,
                    COUNT(*) AS pending_rows
                FROM trades
                WHERE outcome = 'pending'
                """
            ).fetchone()
            daily_row = conn.execute(
                """
                SELECT COALESCE(SUM(pnl_cents), 0) AS daily_realized_pnl_cents
                FROM trades
                WHERE outcome IN ('win', 'loss')
                  AND settled_at IS NOT NULL
                  AND substr(settled_at, 1, 10) = ?
                """,
                (today_utc,),
            ).fetchone()
            return {
                "pending_exposure_cents": int((pending_row[0] if pending_row else 0) or 0),
                "pending_rows": int((pending_row[1] if pending_row else 0) or 0),
                "daily_realized_pnl_cents": int((daily_row[0] if daily_row else 0) or 0),
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _write_trade_guard_snapshot(self) -> None:
        """
        Publish a compact pre-trade guard snapshot for bot_runner fail-safe checks.
        """
        now = datetime.now(timezone.utc)
        today_utc = now.date().isoformat()

        balance_cents = self._last_known_balance_cents
        valid = True
        reason = "ok"
        client = self._get_portfolio_client()
        if client is not None:
            try:
                bal = client.get_balance()
                fetched = int(bal.get("balance", 0) or 0)
                if fetched > 0:
                    balance_cents = fetched
                    self._last_known_balance_cents = fetched
            except Exception as exc:
                if balance_cents <= 0:
                    valid = False
                    reason = f"balance_unavailable:{exc}"
                else:
                    reason = f"balance_stale:{exc}"
        elif balance_cents <= 0:
            valid = False
            reason = "portfolio_client_unavailable"

        bot_metrics: Dict[str, Dict[str, int]] = {}
        total_exposure = 0
        total_daily_pnl = 0
        for bot_name in self.bots:
            db_path = self._bot_learning_db_paths.get(
                bot_name,
                self.project_root / "data" / f"{bot_name}.db",
            )
            try:
                metrics = self._query_bot_trade_metrics(db_path, today_utc)
            except Exception as exc:
                logger.warning("Trade metrics read failed for %s: %s", bot_name, exc)
                metrics = {
                    "pending_exposure_cents": 0,
                    "pending_rows": 0,
                    "daily_realized_pnl_cents": 0,
                }
            bot_metrics[bot_name] = metrics
            total_exposure += int(metrics.get("pending_exposure_cents", 0) or 0)
            total_daily_pnl += int(metrics.get("daily_realized_pnl_cents", 0) or 0)

        limits = {
            "global_daily_loss_limit_cents": int(
                self.swarm_cfg.get("global_daily_loss_limit_cents", 15000) or 0
            ),
            "global_exposure_limit_cents": int(
                self.swarm_cfg.get("global_exposure_limit_cents", 50000) or 0
            ),
        }

        bots_snapshot: Dict[str, Dict[str, Any]] = {}
        for bot_name in self.bots:
            alloc_pct = float(self.balance_manager.get_bot_allocation_pct(bot_name) or 0.0)
            allocated = int(balance_cents * alloc_pct)
            pending = int(bot_metrics.get(bot_name, {}).get("pending_exposure_cents", 0) or 0)
            bots_snapshot[bot_name] = {
                "allocation_pct": round(alloc_pct * 100, 2),
                "allocated_budget_cents": allocated,
                "pending_exposure_cents": pending,
                "available_budget_cents": max(0, allocated - pending),
                "daily_realized_pnl_cents": int(
                    bot_metrics.get(bot_name, {}).get("daily_realized_pnl_cents", 0) or 0
                ),
                "pending_rows": int(bot_metrics.get(bot_name, {}).get("pending_rows", 0) or 0),
            }

        snapshot = {
            "timestamp": now.isoformat(),
            "valid": bool(valid),
            "reason": reason,
            "limits": limits,
            "metrics": {
                "total_balance_cents": int(balance_cents),
                "total_exposure_cents": int(total_exposure),
                "total_daily_pnl_cents": int(total_daily_pnl),
            },
            "bots": bots_snapshot,
        }

        tmp = self._trade_guard_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, indent=2)
            tmp.replace(self._trade_guard_file)
        except Exception as exc:
            logger.warning("Failed writing trade guard snapshot: %s", exc)

    # ------------------------------------------------------------------
    # Portfolio count reconciliation
    # ------------------------------------------------------------------

    def _get_portfolio_client(self) -> Optional[KalshiClient]:
        """Lazily build an authenticated client for account-level count checks."""
        if self._portfolio_client is not None:
            return self._portfolio_client
        try:
            api_cfg = self.cfg.get("api", {})
            key_id = api_cfg.get("key_id") or os.environ.get("KALSHI_KEY_ID", "")
            key_path = Path(api_cfg.get("private_key_path", ""))
            if not key_path.is_absolute():
                key_path = self.project_root / key_path
            if not key_id or not key_path.exists():
                return None
            self._portfolio_client = KalshiClient(
                api_key_id=key_id,
                private_key_path=str(key_path),
                base_url=api_cfg.get("base_url", ""),
                demo_mode=bool(api_cfg.get("demo_mode", False)),
            )
            return self._portfolio_client
        except Exception as exc:
            logger.warning("Portfolio client init failed: %s", exc)
            return None

    @staticmethod
    def _parent_ticker(ticker: str) -> str:
        """Collapse outcome-specific tickers into a parent market identifier."""
        parts = str(ticker or "").split("-")
        return "-".join(parts[:-1]) if len(parts) > 1 else str(ticker or "")

    def _get_market_status_cached(self, client: KalshiClient, ticker: str) -> str:
        now = datetime.now(timezone.utc)
        cached = self._market_status_cache.get(ticker)
        if cached and cached.get("at"):
            age = (now - cached["at"]).total_seconds()
            if age <= self._market_status_ttl_seconds:
                return str(cached.get("status", ""))
        try:
            market = client.get_market(ticker)
            status = str(market.get("status", "")).lower()
            self._market_status_cache[ticker] = {"status": status, "at": now}
            return status
        except Exception:
            return ""

    def _get_position_count_views(self) -> Dict[str, Any]:
        """
        Return both UI-like grouped counts and raw API leg counts.
        Cached briefly to limit API churn from dashboard polling.
        """
        now = datetime.now(timezone.utc)
        cached_at = self._portfolio_counts_cache.get("timestamp")
        if cached_at is not None:
            age = (now - cached_at).total_seconds()
            if age <= self._portfolio_counts_ttl_seconds:
                return dict(self._portfolio_counts_cache.get("counts", {}))

        counts = {
            "ui_open_markets": 0,
            "ui_pending_markets": 0,
            "api_open_legs": 0,
            "api_parent_markets": 0,
        }
        client = self._get_portfolio_client()
        if client is None:
            return counts

        try:
            positions = client.get_positions(count_filter="position")
            open_rows = [
                p for p in positions
                if int(p.get("position", 0) or 0) != 0
            ]
            counts["api_open_legs"] = len(open_rows)

            parent_has_open: Dict[str, bool] = {}
            open_states = {"active", "open", "trading"}
            for row in open_rows:
                ticker = str(row.get("ticker", "")).strip()
                if not ticker:
                    continue
                parent = self._parent_ticker(ticker)
                status = self._get_market_status_cached(client, ticker)
                is_open = status in open_states
                parent_has_open[parent] = bool(parent_has_open.get(parent, False) or is_open)

            counts["api_parent_markets"] = len(parent_has_open)
            counts["ui_open_markets"] = sum(1 for v in parent_has_open.values() if v)
            counts["ui_pending_markets"] = counts["api_parent_markets"] - counts["ui_open_markets"]
        except Exception as exc:
            logger.warning("Failed to compute portfolio count views: %s", exc)

        self._portfolio_counts_cache = {"timestamp": now, "counts": counts}
        return dict(counts)

    # ------------------------------------------------------------------
    # Auto scale
    # ------------------------------------------------------------------

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _rebalance_allocations_with_bounds(
        targets: Dict[str, float],
        min_alloc: float,
        max_alloc: float,
    ) -> Dict[str, float]:
        """Normalize allocations to 100% while honoring per-bot min/max bounds."""
        allocs = {
            name: max(min_alloc, min(max_alloc, float(pct)))
            for name, pct in targets.items()
        }
        for _ in range(8):
            total = sum(allocs.values())
            if abs(total - 1.0) < 1e-6:
                break

            if total > 1.0:
                excess = total - 1.0
                adjustable = [k for k, v in allocs.items() if v > min_alloc + 1e-9]
                capacity = sum(allocs[k] - min_alloc for k in adjustable)
                if capacity <= 0:
                    break
                for k in adjustable:
                    cut = excess * ((allocs[k] - min_alloc) / capacity)
                    allocs[k] = max(min_alloc, allocs[k] - cut)
            else:
                deficit = 1.0 - total
                adjustable = [k for k, v in allocs.items() if v < max_alloc - 1e-9]
                capacity = sum(max_alloc - allocs[k] for k in adjustable)
                if capacity <= 0:
                    break
                for k in adjustable:
                    bump = deficit * ((max_alloc - allocs[k]) / capacity)
                    allocs[k] = min(max_alloc, allocs[k] + bump)

        final_total = sum(allocs.values())
        if final_total > 0:
            allocs = {k: v / final_total for k, v in allocs.items()}
        return allocs

    @staticmethod
    def _rebalance_with_direction_guards(
        proposed: Dict[str, float],
        current: Dict[str, float],
        decisions: Dict[str, Dict[str, Any]],
        min_alloc: float,
        max_alloc: float,
    ) -> Dict[str, float]:
        """
        Rebalance allocations while preserving promote/demote direction intent.

        Guardrails:
        - A demoted bot cannot end up above its current allocation.
        - A promoted bot cannot end up below its current allocation.
        - If a valid 100% rebalance cannot satisfy direction constraints, keep current.
        """
        allocs = {
            bot: max(min_alloc, min(max_alloc, float(proposed.get(bot, current.get(bot, 0.0)))))
            for bot in current
        }

        # First pass: enforce directional bounds against current allocations.
        for bot, old in current.items():
            action = str((decisions.get(bot) or {}).get("action", "hold"))
            if action == "demote" and allocs[bot] > old:
                allocs[bot] = old
            elif action == "promote" and allocs[bot] < old:
                allocs[bot] = old

        # Rebalance to 100% using only direction-compatible bots.
        for _ in range(10):
            total = sum(allocs.values())
            diff = 1.0 - total
            if abs(diff) < 1e-6:
                break

            if diff > 0:
                eligible = [
                    bot
                    for bot, val in allocs.items()
                    if val < max_alloc - 1e-9
                    and str((decisions.get(bot) or {}).get("action", "hold")) != "demote"
                ]
                if not eligible:
                    return dict(current)
                capacity = sum(max_alloc - allocs[bot] for bot in eligible)
                if capacity <= 0:
                    return dict(current)
                for bot in eligible:
                    bump = diff * ((max_alloc - allocs[bot]) / capacity)
                    allocs[bot] = min(max_alloc, allocs[bot] + bump)
            else:
                excess = -diff
                eligible = [
                    bot
                    for bot, val in allocs.items()
                    if val > min_alloc + 1e-9
                    and str((decisions.get(bot) or {}).get("action", "hold")) != "promote"
                ]
                if not eligible:
                    return dict(current)
                capacity = sum(allocs[bot] - min_alloc for bot in eligible)
                if capacity <= 0:
                    return dict(current)
                for bot in eligible:
                    cut = excess * ((allocs[bot] - min_alloc) / capacity)
                    allocs[bot] = max(min_alloc, allocs[bot] - cut)

        # Final directional clamp (defensive against floating-point drift).
        for bot, old in current.items():
            action = str((decisions.get(bot) or {}).get("action", "hold"))
            if action == "demote" and allocs[bot] > old + 1e-9:
                return dict(current)
            if action == "promote" and allocs[bot] < old - 1e-9:
                return dict(current)

        return allocs

    def _evaluate_auto_scale_decision(
        self,
        bot_name: str,
        status: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Return promote/demote/hold decision for one bot based on KPI gates."""
        cfg = self.auto_scale_cfg
        perf = status.get("performance", {}) or {}
        risk = status.get("risk", {}) or {}

        trades = int(perf.get("total_trades", 0) or 0)
        min_trades = int(cfg.get("min_trades_for_scoring", 20))
        if trades < min_trades:
            return {
                "action": "hold",
                "reason": f"insufficient_trades:{trades}<{min_trades}",
            }

        win_rate = self._as_float(perf.get("win_rate", 0.0))
        roi_pct = self._as_float(perf.get("roi_pct", 0.0))
        sharpe = self._as_float(perf.get("sharpe", 0.0))
        total_pnl = int(perf.get("total_pnl", 0) or 0)
        drawdown_pct = self._as_float(risk.get("drawdown_pct", 0.0))
        pause_remaining = int(risk.get("drawdown_pause_remaining_seconds", 0) or 0)
        can_trade = bool(risk.get("can_trade", False))

        promote_checks = [
            ("win_rate", win_rate >= self._as_float(cfg.get("promote_min_win_rate_pct", 56.0))),
            ("roi", roi_pct >= self._as_float(cfg.get("promote_min_roi_pct", 0.8))),
            ("sharpe", sharpe >= self._as_float(cfg.get("promote_min_sharpe", 0.08))),
            ("pnl", total_pnl >= int(cfg.get("promote_min_total_pnl_cents", 100))),
            ("drawdown", drawdown_pct <= self._as_float(cfg.get("promote_max_drawdown_pct", 6.0))),
        ]
        if bool(cfg.get("require_can_trade_for_promotion", True)):
            promote_checks.append(("can_trade", can_trade))

        demote_checks = [
            ("win_rate", win_rate < self._as_float(cfg.get("demote_win_rate_below_pct", 48.0))),
            ("roi", roi_pct < self._as_float(cfg.get("demote_roi_below_pct", -0.5))),
            ("sharpe", sharpe < self._as_float(cfg.get("demote_sharpe_below", -0.05))),
            ("pnl", total_pnl < int(cfg.get("demote_total_pnl_below_cents", -100))),
            ("drawdown", drawdown_pct >= self._as_float(cfg.get("demote_drawdown_above_pct", 10.0))),
            ("cooldown", pause_remaining > 0),
        ]

        promote_ok = all(ok for _, ok in promote_checks)
        demote_hit = any(hit for _, hit in demote_checks)

        if promote_ok and not demote_hit:
            return {"action": "promote", "reason": "kpi_green"}
        if demote_hit:
            failed = [name for name, hit in demote_checks if hit]
            return {"action": "demote", "reason": "kpi_red:" + ",".join(failed)}
        return {"action": "hold", "reason": "kpi_neutral"}

    def _run_auto_scale(self) -> None:
        """Evaluate KPI gates and adjust bot budget allocations automatically."""
        if not bool(self.auto_scale_cfg.get("enabled", False)):
            return

        now = datetime.now(timezone.utc)
        interval = max(60, int(self.auto_scale_cfg.get("evaluation_interval_seconds", 300)))
        if self._last_auto_scale_at is not None:
            elapsed = (now - self._last_auto_scale_at).total_seconds()
            if elapsed < interval:
                return
        self._last_auto_scale_at = now

        promote_step = self._as_float(self.auto_scale_cfg.get("promote_step_pct", 2.5)) / 100.0
        demote_step = self._as_float(self.auto_scale_cfg.get("demote_step_pct", 5.0)) / 100.0
        min_alloc = self._as_float(self.auto_scale_cfg.get("min_allocation_pct", 10.0)) / 100.0
        max_alloc = self._as_float(self.auto_scale_cfg.get("max_allocation_pct", 40.0)) / 100.0
        min_change = self._as_float(self.auto_scale_cfg.get("min_change_to_apply_pct", 0.5)) / 100.0

        current = {
            bot_name: self.balance_manager.get_bot_allocation_pct(bot_name)
            for bot_name in self.bots
        }
        targets = dict(current)
        decisions: Dict[str, Dict[str, Any]] = {}

        for bot_name, bot in self.bots.items():
            status = self._read_bot_status(bot_name)
            if not self._status_matches_process(bot, status):
                decisions[bot_name] = {"action": "hold", "reason": "no_live_status"}
                continue
            decision = self._evaluate_auto_scale_decision(bot_name, status)
            decisions[bot_name] = decision
            if decision["action"] == "promote":
                targets[bot_name] = targets.get(bot_name, 0.0) + promote_step
            elif decision["action"] == "demote":
                targets[bot_name] = targets.get(bot_name, 0.0) - demote_step

        bounded = self._rebalance_allocations_with_bounds(targets, min_alloc, max_alloc)
        final_allocs = self._rebalance_with_direction_guards(
            proposed=bounded,
            current=current,
            decisions=decisions,
            min_alloc=min_alloc,
            max_alloc=max_alloc,
        )
        changes: List[Dict[str, Any]] = []
        for bot_name in self.bots:
            old = current.get(bot_name, 0.0)
            new = final_allocs.get(bot_name, old)
            delta = new - old
            if abs(delta) < min_change:
                continue
            self.balance_manager.set_bot_allocation(bot_name, new)
            info = {
                "bot": bot_name,
                "old_pct": round(old * 100, 2),
                "new_pct": round(new * 100, 2),
                "delta_pct": round(delta * 100, 2),
                "decision": decisions.get(bot_name, {}).get("action", "hold"),
                "reason": decisions.get(bot_name, {}).get("reason", ""),
            }
            changes.append(info)
            self._log_activity(
                bot_name,
                "auto_scale",
                f"{info['old_pct']}% -> {info['new_pct']}% ({info['reason']})",
            )

        self._last_auto_scale_summary = {
            "enabled": True,
            "last_evaluated_at": now.isoformat(),
            "changes": changes,
            "decisions": decisions,
            "reason": "applied" if changes else "no_material_change",
        }
        if changes:
            logger.info("Auto-scale applied %d allocation change(s): %s", len(changes), changes)

    # ------------------------------------------------------------------
    # Status aggregation
    # ------------------------------------------------------------------

    def get_swarm_status(self) -> Dict[str, Any]:
        """Return comprehensive swarm status for the dashboard."""
        bot_statuses = {}
        for bot_name in self.bots:
            status = self._read_bot_status(bot_name)
            bot = self.bots[bot_name]
            if self._status_matches_process(bot, status):
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

        position_counts = self._get_position_count_views()
        position_counts["engine_pending_rows"] = sum(
            int(s.get("pending_trades", 0) or 0)
            for s in bot_statuses.values()
        )
        position_counts["engine_open_positions_sum"] = sum(
            int((s.get("risk", {}) or {}).get("open_positions", 0) or 0)
            for s in bot_statuses.values()
        )

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
            "auto_scale": self._last_auto_scale_summary,
            "position_counts": position_counts,
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

        self.tg_bot.start()
        self._write_trade_guard_snapshot()
        self.start_all()
        self._write_trade_guard_snapshot()

        check_interval = self.swarm_cfg.get(
            "health_check_interval_seconds",
            self.swarm_cfg.get("health_check_interval", 60),
        )

        while self._running:
            try:
                self.check_health()
                self.conflict_resolver.prune_stale_claims()
                self._run_auto_scale()
                self._write_trade_guard_snapshot()
                time.sleep(check_interval)
            except Exception as exc:
                logger.exception("Coordinator error: %s", exc)
                time.sleep(10)

        self._shutdown()

    def _shutdown(self) -> None:
        """Graceful shutdown of the entire swarm."""
        logger.info("Shutting down swarm...")
        self.tg_bot.stop()
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

