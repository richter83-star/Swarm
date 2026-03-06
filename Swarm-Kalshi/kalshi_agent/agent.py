"""
agent.py
========

Main orchestrator. Improvements over the original:

1. Analysis engine is wired to the learning engine for category multipliers
   and calibrated confidence thresholds.
2. Market enrichment now fetches recent trades alongside the orderbook so
   the momentum score has real data.
3. Trade logging includes series_ticker and category for per-category learning.
4. Trend snapshot is computed on each review cycle and logged for visibility.
5. Graceful shutdown and daily summary unchanged from original.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from kalshi_agent.analysis_engine import AnalysisEngine, TradeSignal
from kalshi_agent.dashboard import Dashboard
from kalshi_agent.human_behavior import HumanBehavior
from kalshi_agent.kalshi_client import KalshiClient, KalshiAPIError
from kalshi_agent.learning_engine import LearningEngine
from kalshi_agent.market_scanner import MarketScanner
from kalshi_agent.risk_manager import RiskManager

logger = logging.getLogger("kalshi_agent")


class TradingAgent:
    """
    Autonomous trading agent for the Kalshi prediction-markets exchange.
    """

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self.cfg = self._load_config()
        self._setup_logging()
        self._running = True

        api_cfg = self.cfg["api"]
        self.client = KalshiClient(
            api_key_id=api_cfg["key_id"],
            private_key_path=api_cfg["private_key_path"],
            base_url=api_cfg["base_url"],
            demo_mode=api_cfg.get("demo_mode", True),
        )

        trading_cfg = self.cfg.get("trading", {})
        risk_cfg = {**trading_cfg, **self.cfg.get("risk", {})}

        self.scanner = MarketScanner(self.client, trading_cfg)
        self.learning = LearningEngine(self.cfg.get("learning", {}))
        # Wire learning engine into analysis engine for category-aware scoring.
        self.analysis = AnalysisEngine(
            trading_cfg, learning_engine=self.learning
        )
        self.behavior = HumanBehavior(self.cfg.get("human_behavior", {}))
        self.risk = RiskManager(risk_cfg)
        self.dashboard = Dashboard(self.learning)

        self._pending_trades: Dict[str, int] = {}

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ------------------------------------------------------------------
    # Configuration & logging
    # ------------------------------------------------------------------

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            logger.error("Configuration file not found: %s", self.config_path)
            sys.exit(1)
        with open(self.config_path) as fh:
            return yaml.safe_load(fh)

    def _setup_logging(self) -> None:
        log_cfg = self.cfg.get("logging", {})
        level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

        log_file = log_cfg.get("file", "logs/kalshi_agent.log")
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get("max_bytes", 10_485_760),
            backupCount=log_cfg.get("backup_count", 5),
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
        logger.setLevel(level)

    def _handle_signal(self, signum, frame):
        logger.info("Received signal %d — shutting down gracefully …", signum)
        self._running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        logger.info("=" * 60)
        logger.info("Kalshi AI Trading Agent v2.0 starting up.")
        logger.info("Base URL : %s", self.cfg["api"]["base_url"])
        logger.info("Demo mode: %s", self.cfg["api"].get("demo_mode", True))
        logger.info("=" * 60)

        while self._running:
            try:
                self._run_cycle()
            except KalshiAPIError as exc:
                logger.error("API error during cycle: %s", exc)
                time.sleep(30)
            except Exception as exc:
                logger.exception("Unexpected error during cycle: %s", exc)
                time.sleep(60)

        self._shutdown()

    def _run_cycle(self) -> None:
        if not self.behavior.state.is_active:
            if self.behavior.should_start_session():
                self.behavior.start_session()
            else:
                self.behavior.idle_wait()
                return

        if self.behavior.should_end_session():
            self._end_of_session()
            self.behavior.end_session()
            self.behavior.idle_wait()
            return

        self._refresh_state()

        if not self.risk.can_trade():
            logger.info("Risk manager says NO. Waiting …")
            self.behavior.long_pause()
            return

        self.behavior.wait()
        opportunities = self.scanner.scan()
        if not opportunities:
            logger.info("No opportunities found this scan.")
            self.behavior.wait()
            return

        if self.behavior.should_browse_only():
            import random
            browse_target = random.choice(opportunities)
            self.scanner.enrich(browse_target)
            logger.info("Browsed %s without trading.", browse_target.ticker)
            self.behavior.record_action(traded=False)
            self.behavior.wait()
            return

        # Enrich top-N with orderbook + recent trades for momentum scoring.
        top_n = min(10, len(opportunities))
        for opp in opportunities[:top_n]:
            self.scanner.enrich(opp)  # fetches orderbook AND recent trades
            self.behavior.wait()

        signals = self.analysis.analyse(opportunities[:top_n])
        if not signals:
            logger.info("No signals above confidence threshold.")
            self.behavior.record_action(traded=False)
            self.behavior.wait()
            return

        best = signals[0]
        self._execute_trade(best)
        self._reconcile_outcomes()

        if self.learning.should_review():
            new_weights = self.learning.review_and_recalibrate(self.analysis.weights)
            self.analysis.update_weights(new_weights)
            trend = self.learning.trend
            logger.info(
                "Trend after review: WR_delta=%.1f%% mult=%.2f hot=%s cold=%s",
                trend.win_rate_trend * 100,
                trend.momentum_multiplier,
                trend.hot_categories,
                trend.cold_categories,
            )

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------

    def _execute_trade(self, signal: TradeSignal) -> None:
        base_count = self.risk.position_size(signal.confidence, signal.suggested_price)
        # Apply momentum multiplier from trend analysis.
        trend_mult = self.learning.trend.momentum_multiplier
        base_count = max(1, int(base_count * trend_mult))
        count = self.behavior.vary_trade_size(base_count)

        logger.info(
            "Executing: %s %s on %s | conf=%.1f | price=%d¢ | count=%d | trend_mult=%.2f",
            signal.action, signal.side, signal.ticker,
            signal.confidence, signal.suggested_price, count, trend_mult,
        )

        self.behavior.order_jitter()

        try:
            order_type = self.cfg.get("trading", {}).get("default_order_type", "limit")
            price_kwarg = {}
            if signal.side == "yes":
                price_kwarg["yes_price"] = signal.suggested_price
            else:
                price_kwarg["no_price"] = signal.suggested_price

            result = self.client.create_order(
                ticker=signal.ticker,
                side=signal.side,
                action=signal.action,
                count=count,
                order_type=order_type,
                **price_kwarg,
            )

            order = result.get("order", result)
            order_id = order.get("order_id", "unknown")
            status = order.get("status", "unknown")
            logger.info("Order placed: id=%s status=%s", order_id, status)

            db_id = self.learning.log_trade(
                ticker=signal.ticker,
                event_ticker=signal.event_ticker,
                title=signal.title,
                side=signal.side,
                action=signal.action,
                count=count,
                entry_price=signal.suggested_price,
                confidence=signal.confidence,
                edge_score=signal.edge_score,
                liquidity_score=signal.liquidity_score,
                volume_score=signal.volume_score,
                timing_score=signal.timing_score,
                momentum_score=signal.momentum_score,
                rationale=signal.rationale,
                series_ticker=signal.ticker.split("-")[0] if "-" in signal.ticker else signal.ticker,
                category=signal.category,
            )
            self._pending_trades[signal.ticker] = db_id
            self.behavior.record_action(traded=True)

        except KalshiAPIError as exc:
            logger.error("Order failed for %s: %s", signal.ticker, exc)
            self.behavior.record_action(traded=False)

    # ------------------------------------------------------------------
    # Outcome reconciliation
    # ------------------------------------------------------------------

    def _reconcile_outcomes(self) -> None:
        if not self._pending_trades:
            return

        try:
            positions = self.client.get_positions()
            pos_by_ticker = {p["ticker"]: p for p in positions}

            settlements = self.client.get_settlements()
            settled_tickers = {s.get("ticker") or s.get("market_ticker") for s in settlements}

            resolved = []
            for ticker, db_id in list(self._pending_trades.items()):
                if ticker in settled_tickers:
                    pos = pos_by_ticker.get(ticker, {})
                    pnl = pos.get("realized_pnl", 0)
                    outcome = "win" if pnl >= 0 else "loss"
                    self.learning.update_outcome(db_id, outcome, pnl_cents=pnl)
                    self.risk.record_outcome(pnl)
                    resolved.append(ticker)
                    logger.info("Resolved %s: %s (%+d¢)", ticker, outcome, pnl)

            for t in resolved:
                del self._pending_trades[t]

        except Exception as exc:
            logger.warning("Reconciliation error: %s", exc)

    # ------------------------------------------------------------------
    # State refresh
    # ------------------------------------------------------------------

    def _refresh_state(self) -> None:
        try:
            balance_data = self.client.get_balance()
            balance = balance_data.get("balance", 0)
            self.risk.update_balance(balance)
            logger.debug("Balance: %d¢ ($%.2f)", balance, balance / 100)
        except KalshiAPIError as exc:
            logger.warning("Failed to fetch balance: %s", exc)

        try:
            positions = self.client.get_positions(count_filter="position")
            self.risk.update_open_positions(len(positions))
        except KalshiAPIError as exc:
            logger.warning("Failed to fetch positions: %s", exc)

    # ------------------------------------------------------------------
    # End-of-session tasks
    # ------------------------------------------------------------------

    def _end_of_session(self) -> None:
        status = self.risk.status()
        perf = self.learning.get_performance(last_n=self.learning.cfg.get("rolling_window", 50))
        self.learning.save_daily_summary(
            pnl_cents=status["daily_pnl_cents"],
            trades=status["trades_today"],
            wins=status["wins_today"],
            losses=status["losses_today"],
            avg_conf=perf.get("avg_confidence", 0),
        )
        try:
            self.dashboard.save_text_report()
        except Exception as exc:
            logger.warning("Report generation failed: %s", exc)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        logger.info("Shutting down …")
        self._end_of_session()
        self.learning.close()
        logger.info("Goodbye.")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi AI Trading Agent")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()

    agent = TradingAgent(config_path=args.config)

    if args.report_only:
        print(agent.dashboard.generate_text_report())
        agent.learning.close()
        return

    agent.run()


if __name__ == "__main__":
    main()
