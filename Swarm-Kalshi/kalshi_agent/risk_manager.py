"""
risk_manager.py
===============

Enforces position-sizing rules, drawdown protection, and daily loss limits.
The module ensures the agent never risks more than the existing account
balance and automatically throttles or pauses trading during losing streaks.

Design principles
-----------------
* **Never deposit more** — all sizing is relative to the current balance.
* **Confidence-scaled sizing** — higher confidence → larger position.
* **Streak awareness** — consecutive losses shrink position sizes.
* **Hard daily cap** — trading halts when the daily loss limit is hit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Daily P&L tracker
# ---------------------------------------------------------------------------

@dataclass
class DailyPnL:
    """Tracks profit and loss for the current calendar day (UTC)."""

    date: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    gross_pnl_cents: int = 0
    trades_today: int = 0
    wins: int = 0
    losses: int = 0

    def reset_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.date:
            logger.info(
                "New trading day. Previous day P&L: %+d¢ (%d trades, %d W / %d L).",
                self.gross_pnl_cents, self.trades_today, self.wins, self.losses,
            )
            self.date = today
            self.gross_pnl_cents = 0
            self.trades_today = 0
            self.wins = 0
            self.losses = 0


# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------

class RiskManager:
    """
    Central risk gate that the orchestrator consults before every trade.

    Parameters
    ----------
    config : dict
        Merged ``trading`` + ``risk`` sections of ``config.yaml``.
    """

    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        self.daily = DailyPnL()
        self._consecutive_losses: int = 0
        self._peak_balance_cents: int = 0
        self._current_balance_cents: int = 0
        self._open_position_count: int = 0

    # ------------------------------------------------------------------
    # Balance updates
    # ------------------------------------------------------------------

    def update_balance(self, balance_cents: int) -> None:
        """Call after every balance fetch to track drawdown."""
        self._current_balance_cents = balance_cents
        if balance_cents > self._peak_balance_cents:
            self._peak_balance_cents = balance_cents

    def update_open_positions(self, count: int) -> None:
        """Update the count of currently open positions."""
        self._open_position_count = count

    # ------------------------------------------------------------------
    # Trade outcome recording
    # ------------------------------------------------------------------

    def record_outcome(self, pnl_cents: int) -> None:
        """
        Record the P&L of a completed trade.

        Parameters
        ----------
        pnl_cents : int
            Positive for a win, negative for a loss.
        """
        self.daily.reset_if_new_day()
        self.daily.gross_pnl_cents += pnl_cents
        self.daily.trades_today += 1

        if pnl_cents >= 0:
            self.daily.wins += 1
            self._consecutive_losses = 0
        else:
            self.daily.losses += 1
            self._consecutive_losses += 1

        logger.info(
            "Trade outcome: %+d¢ | Day P&L: %+d¢ | Streak: %d consecutive %s",
            pnl_cents,
            self.daily.gross_pnl_cents,
            self._consecutive_losses if pnl_cents < 0 else 0,
            "losses" if pnl_cents < 0 else "wins",
        )

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def can_trade(self) -> bool:
        """
        Return *True* only if all risk conditions allow a new trade.

        Checks (in order):
        1. Minimum balance threshold.
        2. Daily loss limit.
        3. Maximum trades per day.
        4. Maximum open positions.
        5. Maximum drawdown from peak.
        """
        self.daily.reset_if_new_day()

        min_bal = self.cfg.get("min_balance_cents", 500)
        if self._current_balance_cents < min_bal:
            logger.warning("Balance %d¢ below minimum %d¢. Trading paused.", self._current_balance_cents, min_bal)
            return False

        daily_limit = self.cfg.get("daily_loss_limit_cents", 5000)
        if self.daily.gross_pnl_cents <= -daily_limit:
            logger.warning("Daily loss limit reached (%+d¢). Trading paused.", self.daily.gross_pnl_cents)
            return False

        max_trades = self.cfg.get("max_trades_per_day", 20)
        if self.daily.trades_today >= max_trades:
            logger.info("Max trades per day (%d) reached.", max_trades)
            return False

        max_pos = self.cfg.get("max_open_positions", 10)
        if self._open_position_count >= max_pos:
            logger.info("Max open positions (%d) reached.", max_pos)
            return False

        max_dd = self.cfg.get("max_drawdown_pct", 0.10)
        if self._peak_balance_cents > 0:
            dd = 1.0 - (self._current_balance_cents / self._peak_balance_cents)
            if dd >= max_dd:
                logger.warning("Drawdown %.1f%% exceeds limit %.1f%%. Trading paused.", dd * 100, max_dd * 100)
                return False

        return True

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def position_size(self, confidence: float, price_cents: int) -> int:
        """
        Compute the number of contracts to buy.

        Sizing logic:
        1. Start with ``max_position_pct`` of the current balance.
        2. Scale linearly by confidence (50 → 0.5×, 100 → 1.0×).
        3. Apply losing-streak multiplier if applicable.
        4. Convert dollar amount to contract count at the given price.
        5. Ensure at least 1 contract.

        Parameters
        ----------
        confidence : float
            Confidence score (0–100).
        price_cents : int
            Per-contract cost in cents.

        Returns
        -------
        int
            Number of contracts to buy.
        """
        if price_cents <= 0:
            return 1

        max_pct = self.cfg.get("max_position_pct", 0.05)
        max_spend = self._current_balance_cents * max_pct

        # Scale by confidence (linear from 0.3× at 50 to 1.0× at 100).
        conf_factor = max(0.3, min(1.0, (confidence - 30) / 70.0))
        spend = max_spend * conf_factor

        # Losing-streak reduction.
        streak_thresh = self.cfg.get("loss_streak_threshold", 3)
        if self._consecutive_losses >= streak_thresh:
            mult = self.cfg.get("loss_streak_size_multiplier", 0.5)
            spend *= mult
            logger.info(
                "Losing streak (%d). Position size reduced by %.0f%%.",
                self._consecutive_losses, (1 - mult) * 100,
            )

        count = int(spend / price_cents)
        return max(1, count)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of the current risk state."""
        self.daily.reset_if_new_day()
        dd = 0.0
        if self._peak_balance_cents > 0:
            dd = 1.0 - (self._current_balance_cents / self._peak_balance_cents)
        return {
            "balance_cents": self._current_balance_cents,
            "peak_balance_cents": self._peak_balance_cents,
            "drawdown_pct": round(dd * 100, 2),
            "daily_pnl_cents": self.daily.gross_pnl_cents,
            "trades_today": self.daily.trades_today,
            "wins_today": self.daily.wins,
            "losses_today": self.daily.losses,
            "consecutive_losses": self._consecutive_losses,
            "open_positions": self._open_position_count,
            "can_trade": self.can_trade(),
        }
