"""
human_behavior.py
=================

Simulates human-like interaction patterns so the trading agent does **not**
exhibit detectable bot behaviour.  Every public method is designed to be
called by the orchestrator before, between, or instead of trading actions.

Key anti-detection strategies
-----------------------------
* **Normally-distributed delays** between actions (not uniform / constant).
* **Variable trade sizes** with a configurable multiplier range.
* **Session-based activity** — the agent works in bursts with idle gaps.
* **Browsing without trading** — occasionally fetches market data with no
  follow-up order, mimicking a human scanning the platform.
* **Randomised login/logout timing** — sessions vary in length and spacing.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SessionState:
    """Tracks the current activity session."""

    session_start: Optional[datetime] = None
    session_end_target: Optional[datetime] = None
    actions_this_session: int = 0
    trades_this_session: int = 0
    browses_this_session: int = 0
    is_active: bool = False
    last_action_time: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Behaviour controller
# ---------------------------------------------------------------------------

class HumanBehavior:
    """
    Provides human-like timing, sizing, and session management.

    Parameters
    ----------
    config : dict
        The ``human_behavior`` section of ``config.yaml``.
    state_file : str, optional
        Path to a JSON file for persisting session state across restarts.
        Defaults to ``data/human_behavior_state.json``.
    """

    def __init__(self, config: Dict[str, Any], state_file: Optional[str] = None):
        self.cfg = config
        self.state = SessionState()
        self._rng = random.Random()
        self._state_file = Path(
            state_file or config.get("state_file", "data/human_behavior_state.json")
        )
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_persisted_state()

    # ------------------------------------------------------------------
    # State persistence across restarts
    # ------------------------------------------------------------------

    def _load_persisted_state(self) -> None:
        """
        Load last_session_end from disk so idle cooldowns survive restarts.
        Without this, a bot that restarts immediately begins a new session,
        which looks unnatural and could trigger bot-detection heuristics.
        """
        import json
        try:
            if self._state_file.exists():
                data = json.loads(self._state_file.read_text())
                last_end_str = data.get("last_session_end")
                if last_end_str:
                    last_end = datetime.fromisoformat(last_end_str)
                    # Reconstruct a fake ended session so idle_wait fires correctly
                    self.state.session_start = last_end
                    self.state.session_end_target = last_end
                    self.state.is_active = False
                    logger.debug(
                        "HumanBehavior: loaded last session end %s from state file.",
                        last_end_str,
                    )
        except Exception as exc:
            logger.debug("HumanBehavior: could not load persisted state: %s", exc)

    def _persist_state(self, last_session_end: datetime) -> None:
        """Persist last session end timestamp to disk."""
        import json
        try:
            self._state_file.write_text(
                json.dumps({"last_session_end": last_session_end.isoformat()})
            )
        except Exception as exc:
            logger.debug("HumanBehavior: could not persist state: %s", exc)

    # ------------------------------------------------------------------
    # Delay helpers
    # ------------------------------------------------------------------

    def action_delay(self) -> float:
        """
        Return a normally-distributed delay (seconds) to wait before the
        next action.  The result is clamped to ``[min, max]``.
        """
        mean = self.cfg.get("action_delay_mean", 8.0)
        std = self.cfg.get("action_delay_std", 3.0)
        lo = self.cfg.get("action_delay_min", 2.0)
        hi = self.cfg.get("action_delay_max", 20.0)
        delay = max(lo, min(hi, self._rng.gauss(mean, std)))
        return delay

    def wait(self) -> None:
        """Sleep for a human-like delay and log it."""
        delay = self.action_delay()
        logger.debug("Human-like delay: %.1fs", delay)
        time.sleep(delay)

    def long_pause(self) -> None:
        """Simulate a longer pause (e.g. reading a page, thinking)."""
        base = self.action_delay()
        multiplier = self._rng.uniform(2.0, 5.0)
        pause = base * multiplier
        logger.debug("Long pause: %.1fs", pause)
        time.sleep(pause)

    # ------------------------------------------------------------------
    # Trade size variation
    # ------------------------------------------------------------------

    def vary_trade_size(self, base_count: int) -> int:
        """
        Return a trade size that varies around *base_count* using a
        uniform multiplier so orders are not identical.
        """
        lo = self.cfg.get("trade_size_min_multiplier", 0.6)
        hi = self.cfg.get("trade_size_max_multiplier", 1.4)
        varied = int(round(base_count * self._rng.uniform(lo, hi)))
        return max(1, varied)

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def should_start_session(self) -> bool:
        """
        Decide whether to begin a new trading session.

        If no session is active, this returns *True* immediately (the idle
        period is enforced by ``idle_wait``).
        """
        if self.state.is_active:
            return False  # already in a session
        return True

    def start_session(self) -> None:
        """Mark the beginning of a new activity session."""
        now = datetime.now(timezone.utc)
        duration_min = self._rng.uniform(
            self.cfg.get("session_duration_min", 15),
            self.cfg.get("session_duration_max", 90),
        )
        from datetime import timedelta

        self.state = SessionState(
            session_start=now,
            session_end_target=now + timedelta(minutes=duration_min),
            is_active=True,
        )
        logger.info(
            "Session started. Target duration: %.0f min (until %s).",
            duration_min,
            self.state.session_end_target.strftime("%H:%M:%S UTC"),
        )

    def should_end_session(self) -> bool:
        """Return *True* if the current session has exceeded its target."""
        if not self.state.is_active:
            return False
        now = datetime.now(timezone.utc)
        if self.state.session_end_target and now >= self.state.session_end_target:
            # Add a small random overshoot so we don't stop exactly on time.
            if self._rng.random() < 0.7:
                return True
        return False

    def end_session(self) -> None:
        """Mark the session as ended and persist the end time."""
        now = datetime.now(timezone.utc)
        logger.info(
            "Session ended after %d actions (%d trades, %d browses).",
            self.state.actions_this_session,
            self.state.trades_this_session,
            self.state.browses_this_session,
        )
        self.state.is_active = False
        self._persist_state(now)

    def idle_wait(self) -> None:
        """Sleep for a randomised idle period between sessions."""
        lo = self.cfg.get("idle_period_min", 30) * 60
        hi = self.cfg.get("idle_period_max", 180) * 60
        idle = self._rng.uniform(lo, hi)
        logger.info("Idle period: %.0f min. Sleeping …", idle / 60)
        time.sleep(idle)

    # ------------------------------------------------------------------
    # Browsing simulation
    # ------------------------------------------------------------------

    def should_browse_only(self) -> bool:
        """
        Randomly decide to browse a market without placing a trade.

        This makes the agent's request pattern look more natural.
        """
        prob = self.cfg.get("browse_without_trade_prob", 0.3)
        return self._rng.random() < prob

    # ------------------------------------------------------------------
    # Action tracking
    # ------------------------------------------------------------------

    def record_action(self, traded: bool = False) -> None:
        """Update session counters after an action."""
        self.state.actions_this_session += 1
        self.state.last_action_time = datetime.now(timezone.utc)
        if traded:
            self.state.trades_this_session += 1
        else:
            self.state.browses_this_session += 1

    # ------------------------------------------------------------------
    # Jitter for order timing
    # ------------------------------------------------------------------

    def order_jitter(self) -> None:
        """
        Tiny random delay (0.5–3 s) injected right before placing an order,
        simulating a human clicking the "confirm" button.
        """
        jitter = self._rng.uniform(0.5, 3.0)
        time.sleep(jitter)

