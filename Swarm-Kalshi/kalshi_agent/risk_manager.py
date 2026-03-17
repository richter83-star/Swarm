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
from datetime import date, datetime, timedelta, timezone
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
        self._drawdown_pause_until: Optional[datetime] = None
        self._last_block_key: Optional[str] = None
        self._last_block_log_at: Optional[datetime] = None

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

        if pnl_cents > 0:
            self.daily.wins += 1
            self._consecutive_losses = 0
            streak_count = 0
            streak_label = "wins"
        elif pnl_cents < 0:
            self.daily.losses += 1
            self._consecutive_losses += 1
            streak_count = self._consecutive_losses
            streak_label = "losses"
        else:
            # Flat result should not be counted as win/loss and should break
            # losing streak pressure for next sizing decisions.
            self._consecutive_losses = 0
            streak_count = 0
            streak_label = "breakeven"

        logger.info(
            "Trade outcome: %+d¢ | Day P&L: %+d¢ | Streak: %d consecutive %s",
            pnl_cents,
            self.daily.gross_pnl_cents,
            streak_count,
            streak_label,
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
        now = datetime.now(timezone.utc)

        min_bal = self.cfg.get("min_balance_cents", 500)
        if self._current_balance_cents < min_bal:
            self._log_block(
                "min_balance",
                "Balance %d¢ below minimum %d¢. Trading paused.",
                self._current_balance_cents,
                min_bal,
            )
            return False

        daily_limit = self.cfg.get("daily_loss_limit_cents", 5000)
        if self.daily.gross_pnl_cents <= -daily_limit:
            self._log_block(
                "daily_loss",
                "Daily loss limit reached (%+d¢). Trading paused.",
                self.daily.gross_pnl_cents,
            )
            return False

        max_trades = self.cfg.get("max_trades_per_day", 20)
        if self.daily.trades_today >= max_trades:
            self._log_block("max_trades", "Max trades per day (%d) reached.", max_trades, level="info")
            return False

        max_pos = self.cfg.get("max_open_positions", 10)
        if self._open_position_count >= max_pos:
            self._log_block("max_positions", "Max open positions (%d) reached.", max_pos, level="info")
            return False

        max_dd = self.cfg.get("max_drawdown_pct", 0.10)
        cooldown_min = int(self.cfg.get("drawdown_pause_cooldown_minutes", 0))
        auto_reset_peak = bool(self.cfg.get("drawdown_auto_reset_peak_after_cooldown", False))
        requires_flat = bool(self.cfg.get("drawdown_auto_reset_requires_flat_positions", True))
        cooldown_extend_min = max(1, int(self.cfg.get("drawdown_cooldown_extend_minutes", 15)))
        reentry_buffer = max(0.0, float(self.cfg.get("drawdown_reentry_buffer_pct", 0.0)))
        resume_dd = max(0.0, float(max_dd) - reentry_buffer)

        if self._peak_balance_cents > 0:
            dd = 1.0 - (self._current_balance_cents / self._peak_balance_cents)
            if self._drawdown_pause_until and now < self._drawdown_pause_until:
                remaining = int((self._drawdown_pause_until - now).total_seconds())
                self._log_block(
                    "drawdown_cooldown",
                    "Drawdown cooldown active (%ds remaining). Trading paused.",
                    max(0, remaining),
                )
                return False

            if self._drawdown_pause_until and now >= self._drawdown_pause_until:
                self._drawdown_pause_until = None
                if auto_reset_peak:
                    if requires_flat and self._open_position_count > 0:
                        self._drawdown_pause_until = now + timedelta(minutes=cooldown_extend_min)
                        self._log_block(
                            "drawdown_wait_flat",
                            "Drawdown cooldown elapsed, but %d open positions remain. Extending pause by %d min.",
                            self._open_position_count,
                            cooldown_extend_min,
                        )
                        return False
                    self._peak_balance_cents = max(1, self._current_balance_cents)
                    self._log_block(
                        "drawdown_auto_reset",
                        "Drawdown cooldown elapsed. Peak reset to %d¢; trading resumed.",
                        self._peak_balance_cents,
                    )
                    self._clear_block()
                    return True

                if dd >= resume_dd:
                    if cooldown_min > 0:
                        self._drawdown_pause_until = now + timedelta(minutes=cooldown_min)
                        self._log_block(
                            "drawdown_still_high",
                            "Drawdown %.1f%% still above resume threshold %.1f%%. Pausing for %d min.",
                            dd * 100,
                            resume_dd * 100,
                            cooldown_min,
                        )
                        return False
                    self._log_block(
                        "drawdown_still_high",
                        "Drawdown %.1f%% still above resume threshold %.1f%%. Trading paused.",
                        dd * 100,
                        resume_dd * 100,
                    )
                    return False

            if dd >= max_dd:
                if cooldown_min > 0:
                    self._drawdown_pause_until = now + timedelta(minutes=cooldown_min)
                    self._log_block(
                        "drawdown_limit",
                        "Drawdown %.1f%% exceeds limit %.1f%%. Trading paused for %d min.",
                        dd * 100,
                        max_dd * 100,
                        cooldown_min,
                    )
                else:
                    self._log_block(
                        "drawdown_limit",
                        "Drawdown %.1f%% exceeds limit %.1f%%. Trading paused.",
                        dd * 100,
                        max_dd * 100,
                    )
                return False

        self._clear_block()
        return True

    def _log_block(self, key: str, msg: str, *args, level: str = "warning") -> None:
        """Log repeated risk blocks with throttling to avoid log spam."""
        now = datetime.now(timezone.utc)
        throttle_seconds = int(self.cfg.get("risk_block_log_throttle_seconds", 30))
        should_log = key != self._last_block_key
        if not should_log and self._last_block_log_at is not None:
            delta = (now - self._last_block_log_at).total_seconds()
            should_log = delta >= throttle_seconds

        if should_log:
            log_fn = logger.info if level == "info" else logger.warning
            log_fn(msg, *args)
            self._last_block_key = key
            self._last_block_log_at = now

    def _clear_block(self) -> None:
        self._last_block_key = None
        self._last_block_log_at = None

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

    def calculate_kelly_size(
        self,
        confidence: float,
        edge: float,
        balance_cents: int,
        implied_market_probability: Optional[float] = None,
        evidence_quality: Optional[float] = None,
    ) -> int:
        """
        Compute position size in cents using fractional Kelly criterion.

        Formula:
          edge_fraction   = (confidence/100) - implied_market_probability
          kelly_fraction  = edge_fraction / (1 - edge_fraction)  if edge > 0 else 0
          fractional_kelly = kelly_fraction * 0.25  (conservative 25% Kelly)
          spend_cents     = balance_cents * fractional_kelly * confidence_multiplier

        Confidence multipliers (combines quantitative confidence + evidence quality):
          conf >= 80 AND quality >= 0.7  → 1.00x
          conf >= 72 AND quality >= 0.5  → 0.75x
          conf >= 72 AND quality < 0.5   → 0.50x
          conf >= 72 AND quality is None → 0.60x
          otherwise                      → 0.40x (conservative floor)

        Position is capped at max_position_pct of balance and floored at
        min_position_cents (default 50¢).

        Parameters
        ----------
        confidence : float
            Quantitative confidence score (0–100).
        edge : float
            Estimated edge in cents (e.g. from TradeSignal.edge).
        balance_cents : int
            Current account balance in cents.
        implied_market_probability : float, optional
            The market's implied YES probability (0–1).  Defaults to 0.50.
        evidence_quality : float, optional
            Evidence quality score from research pipeline (0–1).
            When None, uses the "no evidence" multiplier tier.

        Returns
        -------
        int
            Recommended spend in cents (floor: min_position_cents = 50¢).
        """
        if balance_cents <= 0:
            return 0

        # Sizing config
        sizing_cfg = self.cfg.get("position_sizing", {})
        kelly_fraction_cfg = float(sizing_cfg.get("kelly_fraction", 0.25))
        max_pct = float(
            sizing_cfg.get("max_position_pct")
            or self.cfg.get("max_position_pct", 0.03)
        )
        min_cents = int(sizing_cfg.get("min_position_cents", 50))

        # Implied probability (default 50% = perfectly efficient market)
        impl_prob = float(implied_market_probability) if implied_market_probability is not None else 0.50
        impl_prob = max(0.01, min(0.99, impl_prob))

        # Edge as a probability fraction
        our_prob = max(0.01, min(0.99, confidence / 100.0))
        edge_fraction = our_prob - impl_prob

        if edge_fraction <= 0:
            return 0  # No positive edge

        # Kelly fraction
        raw_kelly = edge_fraction / (1.0 - edge_fraction)
        fractional_kelly = raw_kelly * kelly_fraction_cfg

        # Confidence multiplier based on confidence + evidence quality
        if confidence >= 80 and evidence_quality is not None and evidence_quality >= 0.7:
            multiplier = 1.00
        elif confidence >= 72 and evidence_quality is not None and evidence_quality >= 0.5:
            multiplier = 0.75
        elif confidence >= 72 and evidence_quality is not None and evidence_quality < 0.5:
            multiplier = 0.50
        elif confidence >= 72 and evidence_quality is None:
            multiplier = 0.60
        else:
            multiplier = 0.40

        # Raw spend
        spend_cents = balance_cents * fractional_kelly * multiplier

        # Apply losing-streak reduction
        streak_thresh = int(self.cfg.get("loss_streak_threshold", 3))
        if self._consecutive_losses >= streak_thresh:
            streak_mult = float(self.cfg.get("loss_streak_size_multiplier", 0.5))
            spend_cents *= streak_mult
            logger.info(
                "Kelly sizing: losing streak (%d), applying %.0f%% reduction.",
                self._consecutive_losses, (1 - streak_mult) * 100,
            )

        # Cap at max_position_pct
        max_spend = balance_cents * max_pct
        spend_cents = min(spend_cents, max_spend)

        # Floor at min viable trade
        if spend_cents < min_cents:
            return 0  # Below minimum -- skip trade

        result = int(spend_cents)
        logger.debug(
            "Kelly size: conf=%.1f edge_frac=%.4f kelly=%.4f frac_kelly=%.4f "
            "mult=%.2f spend=%d¢ (max=%d¢)",
            confidence, edge_fraction, raw_kelly, fractional_kelly,
            multiplier, result, int(max_spend),
        )
        return result

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of the current risk state."""
        self.daily.reset_if_new_day()
        dd = 0.0
        if self._peak_balance_cents > 0:
            dd = 1.0 - (self._current_balance_cents / self._peak_balance_cents)
        now = datetime.now(timezone.utc)
        pause_remaining = 0
        if self._drawdown_pause_until and self._drawdown_pause_until > now:
            pause_remaining = int((self._drawdown_pause_until - now).total_seconds())
        return {
            "balance_cents": self._current_balance_cents,
            "peak_balance_cents": self._peak_balance_cents,
            "drawdown_pct": round(dd * 100, 2),
            "drawdown_pause_until": (
                self._drawdown_pause_until.isoformat() if self._drawdown_pause_until else None
            ),
            "drawdown_pause_remaining_seconds": max(0, pause_remaining),
            "daily_pnl_cents": self.daily.gross_pnl_cents,
            "trades_today": self.daily.trades_today,
            "wins_today": self.daily.wins,
            "losses_today": self.daily.losses,
            "consecutive_losses": self._consecutive_losses,
            "open_positions": self._open_position_count,
            "can_trade": self.can_trade(),
        }

    def export_state(self) -> Dict[str, Any]:
        """Serialize risk state for persistence across process restarts."""
        self.daily.reset_if_new_day()
        return {
            "daily": {
                "date": self.daily.date.isoformat(),
                "gross_pnl_cents": int(self.daily.gross_pnl_cents),
                "trades_today": int(self.daily.trades_today),
                "wins": int(self.daily.wins),
                "losses": int(self.daily.losses),
            },
            "consecutive_losses": int(self._consecutive_losses),
            "peak_balance_cents": int(self._peak_balance_cents),
            "current_balance_cents": int(self._current_balance_cents),
            "open_position_count": int(self._open_position_count),
            "drawdown_pause_until": (
                self._drawdown_pause_until.isoformat() if self._drawdown_pause_until else None
            ),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }

    def import_state(self, state: Dict[str, Any]) -> None:
        """Load previously persisted risk state."""
        if not isinstance(state, dict):
            return

        try:
            daily = state.get("daily", {})
            if isinstance(daily, dict):
                d = daily.get("date")
                if d:
                    self.daily.date = date.fromisoformat(str(d))
                self.daily.gross_pnl_cents = int(daily.get("gross_pnl_cents", 0) or 0)
                self.daily.trades_today = int(daily.get("trades_today", 0) or 0)
                self.daily.wins = int(daily.get("wins", 0) or 0)
                self.daily.losses = int(daily.get("losses", 0) or 0)
        except Exception as exc:
            logger.warning("Risk state daily import failed: %s", exc)

        self._consecutive_losses = int(state.get("consecutive_losses", 0) or 0)
        self._peak_balance_cents = int(state.get("peak_balance_cents", 0) or 0)
        self._current_balance_cents = int(state.get("current_balance_cents", 0) or 0)
        self._open_position_count = int(state.get("open_position_count", 0) or 0)

        pause_until = state.get("drawdown_pause_until")
        self._drawdown_pause_until = None
        if pause_until:
            try:
                raw_pause = str(pause_until).strip()
                # Accept both ISO8601 "+00:00" and "Z" suffixes.
                if raw_pause.endswith("Z"):
                    raw_pause = f"{raw_pause[:-1]}+00:00"
                parsed = datetime.fromisoformat(raw_pause)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                else:
                    parsed = parsed.astimezone(timezone.utc)
                self._drawdown_pause_until = parsed
            except Exception as exc:
                logger.warning("Risk state cooldown import failed: %s", exc)

        self._clear_block()
        logger.info(
            "Risk state restored: date=%s pnl=%+d¢ trades=%d peak=%d¢ balance=%d¢ pause_until=%s",
            self.daily.date.isoformat(),
            self.daily.gross_pnl_cents,
            self.daily.trades_today,
            self._peak_balance_cents,
            self._current_balance_cents,
            self._drawdown_pause_until.isoformat() if self._drawdown_pause_until else "none",
        )
