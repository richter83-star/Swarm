"""
telegram/notifier.py
====================

One-way Telegram notifier for the Kalshi bot swarm.

Sends structured alerts for:
  - High-confidence trade signals (pre-execution)
  - Orders placed
  - Trade outcomes (win / loss / expired)
  - Bot crashes and restarts
  - Daily P&L summaries
  - Swarm start / stop events

Uses only the stdlib ``urllib`` — no third-party Telegram SDK required.

Configuration (``telegram`` section in swarm_config.yaml)
---------------------------------------------------------
  enabled: true
  bot_token: ""        # or TELEGRAM_BOT_TOKEN env var
  chat_id: ""          # or TELEGRAM_CHAT_ID env var
  notify_signals: true
  notify_trades: true
  notify_outcomes: true
  notify_crashes: true
  notify_daily_summary: true
  signal_confidence_threshold: 70   # only alert on signals above this
  timeout_seconds: 8
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"

# Emoji palette
_SIDE_EMOJI  = {"yes": "🟢", "no": "🔴"}
_WIN_EMOJI   = "✅"
_LOSS_EMOJI  = "❌"
_EXP_EMOJI   = "⏳"
_SIGNAL_EMOJI = "📡"
_TRADE_EMOJI  = "💸"
_CRASH_EMOJI  = "🚨"
_START_EMOJI  = "🟢"
_STOP_EMOJI   = "🔴"
_PAUSE_EMOJI  = "⏸"
_RESUME_EMOJI = "▶️"
_SUMMARY_EMOJI = "📊"


class TelegramNotifier:
    """
    Sends fire-and-forget messages to a Telegram chat.

    Parameters
    ----------
    config : dict
        The ``telegram`` section of ``swarm_config.yaml``.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        "notify_signals": True,
        "notify_trades": True,
        "notify_outcomes": True,
        "notify_crashes": True,
        "notify_daily_summary": True,
        "signal_confidence_threshold": 70,
        "timeout_seconds": 8,
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}

        # Env vars take priority over config file values
        self._token: str = (
            os.environ.get("TELEGRAM_BOT_TOKEN") or self.cfg.get("bot_token", "")
        )
        self._chat_id: str = (
            os.environ.get("TELEGRAM_CHAT_ID") or str(self.cfg.get("chat_id", ""))
        )

        self._enabled: bool = bool(self.cfg.get("enabled", False))
        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram enabled but bot_token / chat_id missing. "
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars. Disabling."
            )
            self._enabled = False

        # Crash notification cooldown: {bot_name: last_sent_timestamp}
        # Prevents a crash loop from flooding the chat.
        self._crash_last_sent: Dict[str, float] = {}

        if self._enabled:
            logger.info("TelegramNotifier active (chat_id=%s).", self._chat_id)

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    def notify_signal(
        self,
        ticker: str,
        title: str,
        side: str,
        confidence: float,
        price_cents: int,
        bot_name: str,
        rationale: str = "",
    ) -> None:
        """Alert: a high-confidence signal has been identified (pre-execution)."""
        if not self.cfg.get("notify_signals", True):
            return
        threshold = float(self.cfg.get("signal_confidence_threshold", 70))
        if confidence < threshold:
            return

        emoji = _SIDE_EMOJI.get(side, "⚪")
        lines = [
            f"{_SIGNAL_EMOJI} *Signal Identified*",
            f"Bot: `{bot_name}` | Side: {emoji} `{side.upper()}`",
            f"Market: `{ticker}`",
            f"Title: {title}",
            f"Confidence: `{confidence:.1f}%` | Price: `{price_cents}¢`",
        ]
        if rationale:
            lines.append(f"Rationale: _{rationale}_")
        self.send("\n".join(lines))

    def notify_trade(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        confidence: float,
        order_id: str,
        bot_name: str,
    ) -> None:
        """Alert: an order has been placed."""
        if not self.cfg.get("notify_trades", True):
            return
        emoji = _SIDE_EMOJI.get(side, "⚪")
        cost = count * price_cents
        lines = [
            f"{_TRADE_EMOJI} *Order Placed*",
            f"Bot: `{bot_name}` | Side: {emoji} `{side.upper()}`",
            f"Market: `{ticker}`",
            f"Qty: `{count}` × `{price_cents}¢` = `{cost}¢` total",
            f"Confidence: `{confidence:.1f}%` | Order: `{order_id}`",
        ]
        self.send("\n".join(lines))

    def notify_outcome(
        self,
        ticker: str,
        outcome: str,
        pnl_cents: int,
        bot_name: str,
    ) -> None:
        """Alert: a trade has resolved."""
        if not self.cfg.get("notify_outcomes", True):
            return
        if outcome == "win":
            emoji = _WIN_EMOJI
        elif outcome == "loss":
            emoji = _LOSS_EMOJI
        else:
            emoji = _EXP_EMOJI

        sign = "+" if pnl_cents >= 0 else ""
        lines = [
            f"{emoji} *Trade Resolved* — {outcome.upper()}",
            f"Bot: `{bot_name}` | Market: `{ticker}`",
            f"P&L: `{sign}{pnl_cents}¢`",
        ]
        self.send("\n".join(lines))

    def notify_daily_summary(
        self,
        bot_name: str,
        trades: int,
        wins: int,
        losses: int,
        pnl_cents: int,
        win_rate: float,
    ) -> None:
        """Alert: end-of-session daily summary."""
        if not self.cfg.get("notify_daily_summary", True):
            return
        sign = "+" if pnl_cents >= 0 else ""
        lines = [
            f"{_SUMMARY_EMOJI} *Daily Summary* — `{bot_name}`",
            f"Trades: `{trades}` | W/L: `{wins}/{losses}` | WR: `{win_rate:.1f}%`",
            f"P&L: `{sign}{pnl_cents}¢`",
        ]
        self.send("\n".join(lines))

    def notify_crash(self, bot_name: str, exit_code: int) -> None:
        """Alert: a bot process crashed.

        Rate-limited per bot to ``crash_cooldown_minutes`` (default 30) so a
        crash loop cannot flood the Telegram chat.
        """
        if not self.cfg.get("notify_crashes", True):
            return
        cooldown_secs = int(self.cfg.get("crash_cooldown_minutes", 30)) * 60
        last = self._crash_last_sent.get(bot_name, 0)
        if time.time() - last < cooldown_secs:
            logger.debug(
                "notify_crash: %s crash suppressed (cooldown %d min)", bot_name, cooldown_secs // 60
            )
            return
        self._crash_last_sent[bot_name] = time.time()
        self.send(
            f"{_CRASH_EMOJI} *Bot Crashed*\n"
            f"Bot: `{bot_name}` exited with code `{exit_code}`.\n"
            f"Auto-restart in progress."
        )

    def notify_swarm_started(self, bot_names: list) -> None:
        if not self.cfg.get("notify_swarm_started", True):
            return
        bots = ", ".join(f"`{b}`" for b in bot_names)
        self.send(f"{_START_EMOJI} *Swarm Started*\nBots: {bots}")

    def notify_swarm_stopped(self) -> None:
        if not self.cfg.get("notify_swarm_stopped", True):
            return
        self.send(f"{_STOP_EMOJI} *Swarm Stopped*")

    def notify_bot_paused(self, bot_name: str) -> None:
        if not self.cfg.get("notify_bot_paused", True):
            return
        self.send(f"{_PAUSE_EMOJI} Bot `{bot_name}` paused.")

    def notify_bot_resumed(self, bot_name: str) -> None:
        if not self.cfg.get("notify_bot_resumed", True):
            return
        self.send(f"{_RESUME_EMOJI} Bot `{bot_name}` resumed.")

    # ------------------------------------------------------------------
    # Core send
    # ------------------------------------------------------------------

    def send(self, text: str) -> bool:
        """
        Send a Markdown-formatted message to the configured chat.

        Returns True on success, False on failure (never raises).
        """
        if not self._enabled:
            return False
        try:
            url = _API_BASE.format(token=self._token)
            payload = json.dumps({
                "chat_id": self._chat_id,
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
            timeout = int(self.cfg.get("timeout_seconds", 8))
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
            if not result.get("ok"):
                logger.warning("Telegram API error: %s", result)
                return False
            return True
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)
            return False
