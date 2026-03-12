"""
RL feedback bridge to feed settled outcomes into MetaLearner.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class RLFeedbackBridge:
    def record_outcome(
        self,
        meta_learner: Any,
        bot_name: str,
        ticker: str,
        market_title: str,
        market_category: str,
        confidence_at_entry: float,
        outcome: str,
        pnl_cents: int,
        kelly_used: float,
    ) -> None:
        if meta_learner is None:
            return

        try:
            if outcome == "win":
                final_performance = 1.0
            elif outcome == "loss":
                final_performance = 0.0
            else:
                final_performance = 0.5

            conf = max(0.0, min(100.0, float(confidence_at_entry or 0.0)))
            learning_curve = [round(conf / 100.0, 4), float(final_performance)]

            meta_learner.learn_from_task(
                task_type="market_outcome",
                task_description=str(market_title or ticker or "unknown_market"),
                domain=str(market_category or "entertainment"),
                learning_curve=learning_curve,
                final_performance=float(final_performance),
                hyperparameters={
                    "kelly_used": float(kelly_used or 0.0),
                    "confidence_at_entry": conf,
                    "outcome": str(outcome or ""),
                    "pnl_cents": int(pnl_cents or 0),
                    "bot_name": str(bot_name or ""),
                    "ticker": str(ticker or ""),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:
            # Never block trading or reconciliation due to feedback issues.
            logger.warning("RL feedback bridge failed: %s", exc)

