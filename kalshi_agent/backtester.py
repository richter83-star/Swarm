"""
backtester.py
=============

Runs the scoring logic against settled Kalshi markets to pre-load the
learning engine with calibrated trade outcomes before risking real money.

Auto-runs on first launch when the trades table is empty.

Workflow
--------
1. Fetch recently settled markets from the Kalshi API.
2. For each settled market, reconstruct what the analysis engine *would*
   have scored at the time (using last available snapshot data).
3. Determine the actual resolution (YES or NO) and compute hypothetical
   P&L.
4. Log these "backtest trades" into the learning engine so that weight
   calibration, category stats, and confidence calibration have a
   meaningful starting point.
5. Trigger an initial weight recalibration pass.

All backtest trades are tagged with ``action = 'backtest'`` so they can
be distinguished from live trades in the dashboard.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kalshi_agent.kalshi_client import KalshiClient, KalshiAPIError
from kalshi_agent.market_scanner import MarketOpportunity, MarketScanner
from kalshi_agent.analysis_engine import AnalysisEngine
from kalshi_agent.learning_engine import LearningEngine

logger = logging.getLogger(__name__)


class Backtester:
    """
    Pre-loads the learning engine by scoring settled markets.

    Parameters
    ----------
    client : KalshiClient
        Authenticated API client.
    scanner : MarketScanner
        Market scanner instance (used for parsing).
    analysis : AnalysisEngine
        Analysis engine for scoring.
    learning : LearningEngine
        Learning engine to receive backtest trade logs.
    config : dict
        The ``backtester`` section of config (optional overrides).
    """

    DEFAULT_CONFIG = {
        "max_settled_markets": 200,
        "min_volume_24h": 20,
        "auto_run_on_empty_db": True,
        "recalibrate_after": True,
        "backtest_batch_delay": 0.2,
    }

    def __init__(
        self,
        client: KalshiClient,
        scanner: MarketScanner,
        analysis: AnalysisEngine,
        learning: LearningEngine,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.client = client
        self.scanner = scanner
        self.analysis = analysis
        self.learning = learning
        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def should_auto_run(self) -> bool:
        """Return True if the DB is empty and auto-run is enabled."""
        if not self.cfg.get("auto_run_on_empty_db", True):
            return False
        perf = self.learning.get_performance()
        return perf["total_trades"] == 0

    def run(self) -> Dict[str, Any]:
        """
        Execute the backtest: fetch settled markets, score them, log
        hypothetical outcomes, and optionally recalibrate weights.

        Returns a summary dict.
        """
        logger.info("=" * 60)
        logger.info("BACKTESTER: Starting historical backtest run")
        logger.info("=" * 60)

        settled_markets = self._fetch_settled_markets()
        if not settled_markets:
            logger.info("No settled markets found for backtesting.")
            return {"backtested": 0, "wins": 0, "losses": 0, "total_pnl": 0}

        wins = 0
        losses = 0
        total_pnl = 0
        backtested = 0

        for market in settled_markets:
            try:
                result = self._backtest_single(market)
                if result is None:
                    continue

                backtested += 1
                if result["outcome"] == "win":
                    wins += 1
                else:
                    losses += 1
                total_pnl += result["pnl_cents"]

                time.sleep(self.cfg.get("backtest_batch_delay", 0.2))

            except Exception as exc:
                logger.debug("Backtest failed for %s: %s", market.get("ticker"), exc)
                continue

        logger.info(
            "BACKTESTER: Complete. %d markets scored. W/L: %d/%d. P&L: %+d cents",
            backtested, wins, losses, total_pnl,
        )

        # Trigger recalibration if configured
        if self.cfg.get("recalibrate_after", True) and backtested >= 10:
            logger.info("BACKTESTER: Triggering post-backtest weight recalibration.")
            new_weights = self.learning.review_and_recalibrate(
                self.analysis.weights
            )
            self.analysis.update_weights(new_weights)

        return {
            "backtested": backtested,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_settled_markets(self) -> List[Dict[str, Any]]:
        """Fetch recently settled markets from the API."""
        try:
            markets = self.client.get_markets(status="settled")
            max_count = self.cfg.get("max_settled_markets", 200)
            # Filter for markets with enough volume to be meaningful
            min_vol = self.cfg.get("min_volume_24h", 20)
            filtered = [
                m for m in markets
                if (m.get("volume_24h") or m.get("volume") or 0) >= min_vol
            ]
            logger.info(
                "Fetched %d settled markets (%d passed volume filter).",
                len(markets), len(filtered),
            )
            return filtered[:max_count]
        except KalshiAPIError as exc:
            logger.error("Failed to fetch settled markets: %s", exc)
            return []

    def _backtest_single(self, market: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Score a single settled market and log the hypothetical trade.

        Returns a dict with outcome and pnl, or None if the market
        cannot be scored.
        """
        ticker = market.get("ticker", "")
        result_str = market.get("result", "")

        # Determine actual resolution
        if result_str == "yes":
            resolved_yes = True
        elif result_str == "no":
            resolved_yes = False
        elif result_str in ("all_yes", "yes_above", "yes_below"):
            resolved_yes = True
        elif result_str in ("all_no", "no_above", "no_below"):
            resolved_yes = False
        else:
            return None

        # Parse into a MarketOpportunity
        now = datetime.now(timezone.utc)
        opp = self._market_to_opportunity(market, now)
        if opp is None:
            return None

        # Score it
        signals = self.analysis.analyse([opp])
        if not signals:
            # Even if below threshold, create a minimal signal for logging
            side = "yes" if opp.mid_price < 50 else "no"
            confidence = 50.0
            suggested_price = int(opp.mid_price) if side == "yes" else int(100 - opp.mid_price)
            edge_score = 50.0
        else:
            sig = signals[0]
            side = sig.side
            confidence = sig.confidence
            suggested_price = sig.suggested_price
            edge_score = sig.edge_score

        # Determine outcome
        if (side == "yes" and resolved_yes) or (side == "no" and not resolved_yes):
            outcome = "win"
            pnl_cents = 100 - suggested_price  # profit on correct prediction
        else:
            outcome = "loss"
            pnl_cents = -suggested_price  # lost the entry cost

        # Extract category info
        series_ticker = ticker.split("-")[0] if "-" in ticker else ticker
        category = market.get("category", "")

        # Log to learning engine
        db_id = self.learning.log_trade(
            ticker=ticker,
            event_ticker=market.get("event_ticker", ""),
            title=market.get("title", ""),
            side=side,
            action="backtest",
            count=1,
            entry_price=suggested_price,
            confidence=confidence,
            edge_score=edge_score,
            liquidity_score=signals[0].liquidity_score if signals else 50.0,
            volume_score=signals[0].volume_score if signals else 50.0,
            timing_score=signals[0].timing_score if signals else 50.0,
            momentum_score=signals[0].momentum_score if signals else 50.0,
            rationale=f"BACKTEST: resolved={'YES' if resolved_yes else 'NO'}",
            series_ticker=series_ticker,
            category=category,
        )

        # Update outcome immediately
        self.learning.update_outcome(db_id, outcome, exit_price=None, pnl_cents=pnl_cents)

        logger.debug(
            "Backtest %s: side=%s resolved=%s outcome=%s pnl=%+d",
            ticker, side, "YES" if resolved_yes else "NO", outcome, pnl_cents,
        )

        return {"outcome": outcome, "pnl_cents": pnl_cents}

    def _market_to_opportunity(
        self, m: Dict[str, Any], now: datetime
    ) -> Optional[MarketOpportunity]:
        """Convert a raw market dict to a MarketOpportunity for scoring."""
        try:
            yes_bid = m.get("yes_bid") or 0
            yes_ask = m.get("yes_ask") or 0
            no_bid = m.get("no_bid") or 0
            no_ask = m.get("no_ask") or 0
            last_price = m.get("last_price") or 50

            if yes_bid and yes_ask:
                mid = (yes_bid + yes_ask) / 2.0
            else:
                mid = float(last_price)

            if mid <= 0 or mid >= 100:
                return None

            spread = max(0, yes_ask - yes_bid) if yes_bid and yes_ask else 10

            ticker = m.get("ticker", "")
            series_ticker = ticker.split("-")[0] if "-" in ticker else ticker

            return MarketOpportunity(
                ticker=ticker,
                event_ticker=m.get("event_ticker", ""),
                title=m.get("title", ""),
                series_ticker=series_ticker,
                category=m.get("category", ""),
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                last_price=last_price,
                mid_price=mid,
                volume_24h=m.get("volume_24h") or m.get("volume") or 0,
                open_interest=m.get("open_interest") or 0,
                liquidity=m.get("liquidity") or 0,
                spread=spread,
                hours_to_expiry=24.0,  # Use a reasonable default for settled markets
            )
        except Exception as exc:
            logger.debug("Failed to parse settled market %s: %s", m.get("ticker"), exc)
            return None
