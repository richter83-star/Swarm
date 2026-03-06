"""
telegram/bot.py
===============

Two-way Telegram command interface for the Kalshi swarm coordinator.

Runs as a background thread inside ``SwarmCoordinator``.  Uses long-polling
(``getUpdates``) so no webhook or open port is required.

Commands
--------
  /status            — swarm overview (all bots, P&L, state)
  /pause             — pause ALL bots (stop new trades)
  /resume            — resume ALL bots
  /pause <bot>       — pause a specific bot  (e.g. /pause sentinel)
  /resume <bot>      — resume a specific bot
  /stop              — stop the entire swarm (requires confirmation)
  /stop confirm      — confirmed full shutdown
  /pnl               — today's P&L and win-rate per bot
  /help              — show command list

Access control
--------------
Set ``telegram.allowed_user_ids`` to a list of Telegram user IDs that are
permitted to issue commands.  Leave empty to allow any user (not recommended
for a live trading bot).

Configuration (``telegram`` section in swarm_config.yaml)
---------------------------------------------------------
  commands_enabled: true
  allowed_user_ids: []          # e.g. [123456789]
  poll_interval_seconds: 2
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"

BOT_NAMES = ["sentinel", "oracle", "pulse", "vanguard"]


class TelegramCommandBot:
    """
    Long-polling Telegram bot that exposes swarm controls.

    Parameters
    ----------
    config : dict
        The ``telegram`` section of ``swarm_config.yaml``.
    coordinator : SwarmCoordinator
        Live coordinator instance used to action commands.
    project_root : Path
        Project root for reading bot status files.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        coordinator,
        project_root: Path,
    ):
        self.cfg = config
        self.coordinator = coordinator
        self.project_root = project_root

        self._token: str = (
            os.environ.get("TELEGRAM_BOT_TOKEN") or config.get("bot_token", "")
        )
        self._chat_id: str = (
            os.environ.get("TELEGRAM_CHAT_ID") or str(config.get("chat_id", ""))
        )
        self._enabled: bool = (
            bool(config.get("enabled", False))
            and bool(config.get("commands_enabled", True))
            and bool(self._token)
            and bool(self._chat_id)
        )

        raw_ids = config.get("allowed_user_ids", [])
        self._allowed_ids: List[int] = [int(i) for i in raw_ids] if raw_ids else []

        self._poll_interval = float(config.get("poll_interval_seconds", 2))
        self._timeout_seconds = int(config.get("timeout_seconds", 8))
        self._offset: int = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pending_stop_confirm: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start polling in a daemon thread."""
        if not self._enabled:
            logger.debug("TelegramCommandBot disabled — not starting.")
            return
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-cmd-bot",
            daemon=True,
        )
        self._thread.start()
        logger.info("TelegramCommandBot polling started.")

    def stop(self) -> None:
        """Signal the polling thread to exit."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Polling loop
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                updates = self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self._handle_update(update)
            except Exception as exc:
                logger.debug("Telegram poll error: %s", exc)
            self._stop_event.wait(timeout=self._poll_interval)

    def _get_updates(self) -> List[Dict]:
        url = _API.format(token=self._token, method="getUpdates")
        payload = json.dumps({
            "offset": self._offset,
            "timeout": 5,
            "allowed_updates": ["message"],
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout_seconds + 6) as resp:
            data = json.loads(resp.read())
        return data.get("result", []) if data.get("ok") else []

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _handle_update(self, update: Dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        user_id = message.get("from", {}).get("id")
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        if not text.startswith("/"):
            return

        # Access control
        if self._allowed_ids and user_id not in self._allowed_ids:
            self._reply(chat_id, "⛔ Not authorised.")
            logger.warning("Rejected command from user_id=%s: %s", user_id, text)
            return

        parts = text.lstrip("/").split()
        cmd = parts[0].lower().split("@")[0]  # strip bot username suffix
        args = parts[1:]

        logger.info("Telegram command from user %s: /%s %s", user_id, cmd, args)

        handlers = {
            "status":  self._cmd_status,
            "pause":   self._cmd_pause,
            "resume":  self._cmd_resume,
            "stop":    self._cmd_stop,
            "pnl":     self._cmd_pnl,
            "help":    self._cmd_help,
            "start":   self._cmd_help,  # /start from bot initialisation
        }
        handler = handlers.get(cmd)
        if handler:
            try:
                handler(chat_id, args)
            except Exception as exc:
                logger.warning("Command handler error: %s", exc)
                self._reply(chat_id, f"⚠️ Error: {exc}")
        else:
            self._reply(chat_id, f"Unknown command `/{cmd}`. Use /help.")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _cmd_help(self, chat_id: str, _args: List[str]) -> None:
        self._reply(chat_id, (
            "📖 *Kalshi Swarm Commands*\n\n"
            "/status — swarm overview\n"
            "/pnl — today's P&L per bot\n"
            "/pause — pause all bots\n"
            "/pause `<bot>` — pause one bot\n"
            "/resume — resume all bots\n"
            "/resume `<bot>` — resume one bot\n"
            "/stop confirm — stop the entire swarm\n"
            "/help — this message"
        ))

    def _cmd_status(self, chat_id: str, _args: List[str]) -> None:
        lines = ["📊 *Swarm Status*\n"]
        for name in BOT_NAMES:
            status = self._read_status(name)
            state = status.get("state", "unknown")
            perf = status.get("performance", {})
            bal = status.get("balance_cents", 0)
            trades = perf.get("total_trades", 0)
            wr = perf.get("win_rate", 0.0)
            pnl = perf.get("total_pnl", 0)
            sign = "+" if pnl >= 0 else ""
            state_icon = {"running": "🟢", "paused": "⏸", "error": "🔴", "stopped": "⚫"}.get(state, "⚪")
            lines.append(
                f"{state_icon} *{name.capitalize()}* — `{state}`\n"
                f"  Balance: `{bal}¢` | Trades: `{trades}` | "
                f"WR: `{wr:.0f}%` | P&L: `{sign}{pnl}¢`"
            )
        self._reply(chat_id, "\n".join(lines))

    def _cmd_pnl(self, chat_id: str, _args: List[str]) -> None:
        lines = ["💰 *Today's P&L*\n"]
        total_pnl = 0
        for name in BOT_NAMES:
            pnl = self._get_today_pnl(name)
            total_pnl += pnl
            sign = "+" if pnl >= 0 else ""
            emoji = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
            lines.append(f"{emoji} *{name.capitalize()}*: `{sign}{pnl}¢`")
        sign = "+" if total_pnl >= 0 else ""
        lines.append(f"\n*Total: `{sign}{total_pnl}¢`*")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_pause(self, chat_id: str, args: List[str]) -> None:
        if args:
            bot_name = args[0].lower()
            if bot_name not in BOT_NAMES:
                self._reply(chat_id, f"Unknown bot `{bot_name}`. Options: {', '.join(BOT_NAMES)}")
                return
            ok = self.coordinator.pause_bot(bot_name)
            self._reply(chat_id, f"⏸ `{bot_name}` {'paused.' if ok else 'could not be paused.'}")
        else:
            self.coordinator.pause_all()
            self._reply(chat_id, "⏸ All bots paused.")

    def _cmd_resume(self, chat_id: str, args: List[str]) -> None:
        if args:
            bot_name = args[0].lower()
            if bot_name not in BOT_NAMES:
                self._reply(chat_id, f"Unknown bot `{bot_name}`. Options: {', '.join(BOT_NAMES)}")
                return
            ok = self.coordinator.resume_bot(bot_name)
            self._reply(chat_id, f"▶️ `{bot_name}` {'resumed.' if ok else 'could not be resumed.'}")
        else:
            self.coordinator.resume_all()
            self._reply(chat_id, "▶️ All bots resumed.")

    def _cmd_stop(self, chat_id: str, args: List[str]) -> None:
        if args and args[0].lower() == "confirm":
            self._reply(chat_id, "🔴 Stopping swarm now...")
            self.coordinator.stop_all()
            self.coordinator._running = False
        else:
            self._reply(
                chat_id,
                "⚠️ This will stop the entire swarm.\n"
                "Send `/stop confirm` to proceed."
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read_status(self, bot_name: str) -> Dict:
        path = self.project_root / "data" / f"{bot_name}_status.json"
        try:
            if path.exists():
                with open(path) as fh:
                    return json.load(fh)
        except Exception as exc:
            logger.debug("Could not read status for %s: %s", bot_name, exc)
        return {}

    def _get_today_pnl(self, bot_name: str) -> int:
        """Query the bot's SQLite DB for today's settled P&L."""
        cfg_path = self.project_root / "config" / f"{bot_name}_config.yaml"
        try:
            import yaml
            with open(cfg_path) as fh:
                bot_cfg = yaml.safe_load(fh) or {}
            db_path = self.project_root / bot_cfg.get("learning", {}).get("db_path", f"data/{bot_name}.db")
        except Exception:
            db_path = self.project_root / f"data/{bot_name}.db"

        try:
            today = datetime.now(timezone.utc).date().isoformat()
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT gross_pnl_cents FROM daily_summary WHERE date = ?", (today,)
            ).fetchone()
            conn.close()
            return row[0] if row else 0
        except Exception as exc:
            logger.debug("Could not read today pnl for %s: %s", bot_name, exc)
            return 0

    def _reply(self, chat_id: str, text: str) -> None:
        """Send a reply to the given chat."""
        if not self._token:
            return
        try:
            url = _API.format(token=self._token, method="sendMessage")
            payload = json.dumps({
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                json.loads(resp.read())
        except Exception as exc:
            logger.debug("Telegram reply failed: %s", exc)
