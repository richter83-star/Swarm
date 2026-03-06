"""
balance_manager.py
==================

Manages the shared Kalshi account balance across all bots in the swarm.

Each bot is allocated a percentage of the total account balance. The
balance manager tracks virtual allocations and enforces limits so that
no single bot can consume more than its share.

The actual Kalshi account has a single balance -- this module provides
a logical partitioning layer on top of it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class BalanceManager:
    """
    Allocates and tracks balance across swarm bots.

    Parameters
    ----------
    config : dict
        The ``swarm`` section of ``swarm_config.yaml``.
    """

    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self._lock = threading.Lock()
        self._total_balance_cents: int = 0
        self._allocations: Dict[str, float] = dict(
            config.get("budget_allocation", {
                "sentinel": 0.25,
                "oracle": 0.30,
                "pulse": 0.20,
                "vanguard": 0.25,
            })
        )
        self._bot_spent: Dict[str, int] = {}
        self._bot_pnl: Dict[str, int] = {}
        self._last_update: Optional[datetime] = None

        # Validate allocations sum to ~1.0
        total_alloc = sum(self._allocations.values())
        if abs(total_alloc - 1.0) > 0.01:
            logger.warning(
                "Budget allocations sum to %.2f (expected 1.0). Normalizing.",
                total_alloc,
            )
            for k in self._allocations:
                self._allocations[k] /= total_alloc

        for bot_name in self._allocations:
            self._bot_spent[bot_name] = 0
            self._bot_pnl[bot_name] = 0

    # ------------------------------------------------------------------
    # Balance updates
    # ------------------------------------------------------------------

    def update_total_balance(self, balance_cents: int) -> None:
        """Update the total account balance (from API)."""
        with self._lock:
            self._total_balance_cents = balance_cents
            self._last_update = datetime.now(timezone.utc)
            logger.debug("Total balance updated: %d cents ($%.2f)", balance_cents, balance_cents / 100)

    def get_total_balance(self) -> int:
        """Return the current total account balance in cents."""
        with self._lock:
            return self._total_balance_cents

    # ------------------------------------------------------------------
    # Per-bot allocation
    # ------------------------------------------------------------------

    def get_bot_budget(self, bot_name: str) -> int:
        """
        Return the allocated budget for a bot in cents.

        This is the bot's share of the total balance minus what it has
        already spent (unrealized).
        """
        with self._lock:
            alloc_pct = self._allocations.get(bot_name, 0.0)
            allocated = int(self._total_balance_cents * alloc_pct)
            spent = self._bot_spent.get(bot_name, 0)
            return max(0, allocated - spent)

    def get_bot_allocation_pct(self, bot_name: str) -> float:
        """Return the allocation percentage for a bot."""
        return self._allocations.get(bot_name, 0.0)

    def set_bot_allocation(self, bot_name: str, pct: float) -> None:
        """
        Update a bot's allocation percentage.

        Note: does NOT auto-normalize other bots. Call normalize() after
        adjusting multiple bots.
        """
        with self._lock:
            old = self._allocations.get(bot_name, 0.0)
            self._allocations[bot_name] = max(0.0, min(1.0, pct))
            logger.info(
                "Budget allocation for %s changed: %.1f%% -> %.1f%%",
                bot_name, old * 100, pct * 100,
            )

    def normalize_allocations(self) -> None:
        """Normalize all allocations to sum to 1.0."""
        with self._lock:
            total = sum(self._allocations.values())
            if total > 0:
                for k in self._allocations:
                    self._allocations[k] /= total

    # ------------------------------------------------------------------
    # Spending tracking
    # ------------------------------------------------------------------

    def record_spend(self, bot_name: str, amount_cents: int) -> bool:
        """
        Record that a bot is spending (opening a position).

        Returns True if the spend is within budget, False if it would
        exceed the allocation.
        """
        with self._lock:
            budget = self._get_budget_unlocked(bot_name)
            if amount_cents > budget:
                logger.warning(
                    "Bot %s spend %d cents exceeds budget %d cents. Denied.",
                    bot_name, amount_cents, budget,
                )
                return False
            self._bot_spent[bot_name] = self._bot_spent.get(bot_name, 0) + amount_cents
            return True

    def record_return(self, bot_name: str, amount_cents: int) -> None:
        """Record that a bot's position has closed (funds returned)."""
        with self._lock:
            self._bot_spent[bot_name] = max(
                0, self._bot_spent.get(bot_name, 0) - amount_cents
            )

    def record_pnl(self, bot_name: str, pnl_cents: int) -> None:
        """Record realized P&L for a bot."""
        with self._lock:
            self._bot_pnl[bot_name] = self._bot_pnl.get(bot_name, 0) + pnl_cents

    # ------------------------------------------------------------------
    # Global limits
    # ------------------------------------------------------------------

    def get_total_exposure(self) -> int:
        """Return total cents currently at risk across all bots."""
        with self._lock:
            return sum(self._bot_spent.values())

    def check_global_exposure_limit(self) -> bool:
        """Return True if total exposure is within the global limit."""
        limit = self.cfg.get("global_max_exposure_cents", 50000)
        return self.get_total_exposure() < limit

    def get_total_daily_pnl(self) -> int:
        """Return total P&L across all bots."""
        with self._lock:
            return sum(self._bot_pnl.values())

    def check_global_daily_loss_limit(self) -> bool:
        """Return True if total daily P&L is above the loss limit."""
        limit = self.cfg.get("global_daily_loss_limit_cents", 15000)
        return self.get_total_daily_pnl() > -limit

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L counters (call at start of new trading day)."""
        with self._lock:
            for k in self._bot_pnl:
                self._bot_pnl[k] = 0
            logger.info("Daily P&L counters reset for all bots.")

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of the balance manager state."""
        with self._lock:
            bot_status = {}
            for bot_name in self._allocations:
                bot_status[bot_name] = {
                    "allocation_pct": round(self._allocations[bot_name] * 100, 1),
                    "budget_cents": self._get_budget_unlocked(bot_name),
                    "spent_cents": self._bot_spent.get(bot_name, 0),
                    "pnl_cents": self._bot_pnl.get(bot_name, 0),
                }

            return {
                "total_balance_cents": self._total_balance_cents,
                "total_exposure_cents": sum(self._bot_spent.values()),
                "total_pnl_cents": sum(self._bot_pnl.values()),
                "last_update": self._last_update.isoformat() if self._last_update else None,
                "bots": bot_status,
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_budget_unlocked(self, bot_name: str) -> int:
        """Get budget without acquiring lock (caller must hold lock)."""
        alloc_pct = self._allocations.get(bot_name, 0.0)
        allocated = int(self._total_balance_cents * alloc_pct)
        spent = self._bot_spent.get(bot_name, 0)
        return max(0, allocated - spent)
