"""
bot_runner.py
=============

Runs individual bot instances as separate processes within the swarm.

Each bot runner:
- Loads the merged config (swarm defaults + bot-specific overrides)
- Initializes all v2 + v3 modules
- Runs the main trading loop
- Reports status back to the swarm coordinator via shared state files
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_agent.kalshi_client import KalshiClient, KalshiAPIError
from kalshi_agent.market_scanner import MarketScanner
from kalshi_agent.analysis_engine import AnalysisEngine
from kalshi_agent.human_behavior import HumanBehavior
from kalshi_agent.risk_manager import RiskManager
from kalshi_agent.learning_engine import LearningEngine
from kalshi_agent.dashboard import Dashboard
from kalshi_agent.backtester import Backtester
from kalshi_agent.external_signals import ExternalSignals
from kalshi_agent.prior_knowledge import PriorKnowledge
from kalshi_agent.llm_advisor import LLMAdvisor
from telegram.notifier import TelegramNotifier
from swarm.central_llm_controller import CentralLLMController

logger = logging.getLogger("kalshi_agent")


class BotRunner:
    """
    Runs a single specialist bot instance.

    Parameters
    ----------
    bot_name : str
        The bot identifier (e.g., "sentinel", "oracle").
    swarm_config_path : str
        Path to the global swarm config.
    bot_config_path : str
        Path to the bot-specific config overrides.
    project_root : str
        Root directory of the project (for resolving relative paths).
    """

    def __init__(
        self,
        bot_name: str,
        swarm_config_path: str,
        bot_config_path: str,
        project_root: str = ".",
    ):
        self.bot_name = bot_name
        self.project_root = Path(project_root)
        self.cfg = self._load_merged_config(swarm_config_path, bot_config_path)
        self._setup_logging()
        self._running = True
        self._paused = False

        # Status file for coordinator communication
        self._status_file = self.project_root / "data" / f"{bot_name}_status.json"
        self._status_file.parent.mkdir(parents=True, exist_ok=True)
        self._risk_state_file = self.project_root / "data" / f"{bot_name}_risk_state.json"
        self._status_state: str = "starting"
        self._status_error: str = ""
        self._status_lock = threading.Lock()
        self._status_thread: Optional[threading.Thread] = None
        self._status_heartbeat_seconds = max(
            5,
            int(self.cfg.get("swarm", {}).get("status_heartbeat_seconds", 15)),
        )

        # Initialize all modules
        api_cfg = self.cfg["api"]

        # Resolve key_id: config value takes precedence, then env var
        key_id = api_cfg.get("key_id") or os.environ.get("KALSHI_KEY_ID", "")
        if not key_id:
            raise ValueError(
                "Kalshi key_id not found. Set 'api.key_id' in config or "
                "the KALSHI_KEY_ID environment variable."
            )

        # Handle private key path — support both absolute and relative paths
        key_path = Path(api_cfg["private_key_path"])
        if not key_path.is_absolute():
            key_path = self.project_root / key_path
        logger.info("Key path resolved: %s (exists=%s)", key_path, key_path.exists())

        self.client = KalshiClient(
            api_key_id=key_id,
            private_key_path=str(key_path),
            base_url=api_cfg["base_url"],
            demo_mode=api_cfg.get("demo_mode", True),
        )

        trading_cfg = self.cfg.get("trading", {})
        risk_cfg = {**trading_cfg, **self.cfg.get("risk", {})}

        self.scanner = MarketScanner(self.client, trading_cfg)
        self.learning = LearningEngine(self.cfg.get("learning", {}))

        # Initialize prior knowledge
        specialist = self.cfg.get("prior_knowledge", {}).get(
            "specialist",
            self.cfg.get("bot", {}).get("specialist", "general"),
        )
        self.priors = PriorKnowledge(
            specialist=specialist,
            config=self.cfg.get("prior_knowledge", {}),
        )

        # Use prior-seeded weights as starting point
        initial_weights = self.priors.get_initial_weights()

        # v3 modules (must be initialized before AnalysisEngine)
        self.external_signals = ExternalSignals(
            client=self.client,
            config=self.cfg.get("external_signals", {}),
        )

        self.llm_advisor = LLMAdvisor(
            config=self.cfg.get("llm_advisor", {}),
        )

        self.analysis = AnalysisEngine(
            trading_cfg,
            weight_overrides=initial_weights,
            learning_engine=self.learning,
            external_signals=self.external_signals,
            llm_advisor=self.llm_advisor,
        )

        self.behavior = HumanBehavior(
            self.cfg.get("human_behavior", {}),
            state_file=str(self.project_root / "data" / f"{bot_name}_behavior_state.json"),
        )
        self.risk = RiskManager(risk_cfg)
        self._load_risk_state()
        self.dashboard = Dashboard(self.learning)

        self.backtester = Backtester(
            client=self.client,
            scanner=self.scanner,
            analysis=self.analysis,
            learning=self.learning,
            config=self.cfg.get("backtester", {}),
        )

        self.notifier = TelegramNotifier(self.cfg.get("telegram", {}))

        # Centralized LLM controller (Ollama) can approve/reject every trade.
        self.central_llm = CentralLLMController(
            config=self.cfg.get("central_llm", {}),
            project_root=str(self.project_root),
        )

        # Category and keyword filters
        self._category_filters = set(
            c.lower() for c in self.cfg.get("category_filters", [])
        )
        self._excluded_categories = set(
            c.lower() for c in self.cfg.get("excluded_categories", [])
        )
        self._category_keywords = [
            k.lower() for k in self.cfg.get("category_keywords", [])
        ]
        self._series_filters = set(
            s.upper() for s in self.cfg.get("series_filters", [])
        )

        # Track pending trades by stable DB id to avoid losing earlier entries
        # when multiple trades share the same ticker.
        self._pending_trades: Dict[int, Dict[str, Any]] = {}
        self._trade_count = 0
        self._session_start = None
        self._last_backtest_date: Optional[datetime] = None

        # Restore pending trades from DB so outcome reconciliation continues
        # across restarts (required for continuous learning feedback).
        for row in self.learning.get_pending_trades():
            ticker = str(row.get("ticker", "")).strip()
            trade_id = row.get("id")
            if ticker and trade_id is not None:
                trade_id_int = int(trade_id)
                self._pending_trades[trade_id_int] = {
                    "ticker": ticker,
                    "order_id": str(row.get("order_id", "") or ""),
                    "count": int(row.get("count", 1) or 1),
                }
        if self._pending_trades:
            logger.info("Restored %d pending trade(s) from DB.", len(self._pending_trades))
        
        # FIX: Cache last known good balance to prevent $0 displays
        self._last_known_balance: int = 0
        self._last_balance_update: Optional[datetime] = None

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_merged_config(
        self, swarm_path: str, bot_path: str
    ) -> Dict[str, Any]:
        """Load and merge swarm config with bot-specific overrides."""
        with open(swarm_path) as fh:
            base = yaml.safe_load(fh) or {}

        with open(bot_path) as fh:
            overrides = yaml.safe_load(fh) or {}

        return self._deep_merge(base, overrides)

    @staticmethod
    def _deep_merge(base: Dict, override: Dict) -> Dict:
        """Recursively merge override into base."""
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = BotRunner._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _setup_logging(self) -> None:
        log_cfg = self.cfg.get("logging", {})
        level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
        fmt = logging.Formatter(
            f"%(asctime)s | %(levelname)-8s | {self.bot_name} | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

        log_file = log_cfg.get("file", f"logs/{self.bot_name}.log")
        log_path = self.project_root / log_file
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
        logger.info("Received signal %d -- shutting down gracefully.", signum)
        self._running = False

    def _set_status(self, state: str, error: str = "") -> None:
        """Update in-memory status and immediately persist it."""
        self._status_state = state
        self._status_error = error
        self._write_status(state, error)

    def _status_heartbeat_loop(self) -> None:
        """
        Periodically refresh status file even while a long cycle is running.
        This keeps dashboard/coordinator data live under API retries/rate limits.
        """
        while self._running:
            time.sleep(self._status_heartbeat_seconds)
            if not self._running:
                break
            self._write_status(self._status_state, self._status_error)

    def _start_status_heartbeat(self) -> None:
        if self._status_thread and self._status_thread.is_alive():
            return
        self._status_thread = threading.Thread(
            target=self._status_heartbeat_loop,
            daemon=True,
            name=f"{self.bot_name}-status-heartbeat",
        )
        self._status_thread.start()

    def _load_risk_state(self) -> None:
        """Restore persisted risk state from disk if available."""
        if not self._risk_state_file.exists():
            return
        try:
            with open(self._risk_state_file, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self.risk.import_state(state)
            restored = self.risk.export_state()
            daily = restored.get("daily", {})
            logger.info(
                "Loaded persisted risk state from %s (date=%s pnl=%+d¢ trades=%d peak=%d¢ balance=%d¢ pause_until=%s).",
                self._risk_state_file,
                str(daily.get("date", "")),
                int(daily.get("gross_pnl_cents", 0) or 0),
                int(daily.get("trades_today", 0) or 0),
                int(restored.get("peak_balance_cents", 0) or 0),
                int(restored.get("current_balance_cents", 0) or 0),
                restored.get("drawdown_pause_until") or "none",
            )
        except Exception as exc:
            logger.warning("Failed to load risk state file: %s", exc)

    def _save_risk_state(self) -> None:
        """Persist current risk state to disk atomically."""
        try:
            state = self.risk.export_state()
            temp_file = self._risk_state_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            temp_file.replace(self._risk_state_file)
        except Exception as exc:
            logger.warning("Failed to save risk state file: %s", exc)

    # ------------------------------------------------------------------
    # Market filtering
    # ------------------------------------------------------------------

    def _matches_specialist(self, market: Dict[str, Any]) -> bool:
        """Check if a market matches this bot's specialist filters."""
        # If no filters, accept everything (Vanguard behavior)
        if not self._category_filters and not self._series_filters:
            # But check exclusions
            category = (market.get("category") or "").lower()
            if category in self._excluded_categories:
                return False
            return True

        ticker = market.get("ticker", "")
        category = (market.get("category") or "").lower()
        title = (market.get("title") or "").lower()
        series_ticker = ticker.split("-")[0].upper() if "-" in ticker else ticker.upper()

        # Series match
        if self._series_filters and series_ticker in self._series_filters:
            return True

        # Category match
        if category and category in self._category_filters:
            return True

        # Partial category match
        for cat_filter in self._category_filters:
            if cat_filter in category or category in cat_filter:
                return True

        # Keyword match
        if self._category_keywords:
            for kw in self._category_keywords:
                if kw in title:
                    return True

        return False

    def _should_run_weekly_backtest(self) -> bool:
        """Return True if it's been 7+ days since the last backtest run."""
        if self._last_backtest_date is None:
            return False
        from datetime import timedelta
        return (datetime.now(timezone.utc) - self._last_backtest_date) >= timedelta(days=7)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        bot_cfg = self.cfg.get("bot", {})
        display_name = bot_cfg.get("display_name", self.bot_name)
        description = bot_cfg.get("description", "")

        logger.info("=" * 60)
        logger.info("Bot '%s' (%s) starting up.", display_name, self.bot_name)
        logger.info("Specialist: %s", bot_cfg.get("specialist", "general"))
        logger.info("Description: %s", description)
        logger.info("Base URL: %s", self.cfg["api"]["base_url"])
        logger.info("=" * 60)

        # Auto-run backtester if DB is empty
        if self.backtester.should_auto_run():
            logger.info("Empty database detected. Running initial backtester...")
            bt_result = self.backtester.run()
            logger.info("Backtester result: %s", bt_result)
            self._last_backtest_date = datetime.now(timezone.utc)

        self._session_start = datetime.now(timezone.utc)

        # Signal coordinator that backtest is done — do this BEFORE the balance
        # fetch so the coordinator can start the next bot immediately rather than
        # waiting for a potentially rate-limited API call to complete.
        self._set_status("running")
        self._start_status_heartbeat()

        # Fetch real balance so the first status write isn't $0
        try:
            self._refresh_state()
        except Exception as exc:
            logger.warning("Initial balance fetch failed: %s", exc)

        while self._running:
            try:
                if self._paused:
                    self._set_status("paused")
                    time.sleep(5)
                    continue

                # Weekly re-backtest against last 90 days of settled markets
                if self._should_run_weekly_backtest():
                    logger.info("Running weekly backtest recalibration...")
                    bt_result = self.backtester.run()
                    logger.info("Weekly backtest result: %s", bt_result)
                    self._last_backtest_date = datetime.now(timezone.utc)

                self._run_cycle()
                self._set_status("running")

            except KalshiAPIError as exc:
                logger.error("API error during cycle: %s", exc)
                self._set_status("error", str(exc))
                time.sleep(30)
            except Exception as exc:
                logger.exception("Unexpected error during cycle: %s", exc)
                self._set_status("error", str(exc))
                time.sleep(60)

        self._shutdown()

    def _run_cycle(self) -> None:
        """Single trading cycle with multi-signal execution."""
        if not self.behavior.state.is_active:
            if self.behavior.should_start_session():
                self.behavior.start_session()
            else:
                # Refresh balance even during idle so the dashboard never shows $0
                try:
                    self._refresh_state()
                except Exception as exc:
                    logger.warning("Idle balance refresh failed: %s", exc)
                self.behavior.idle_wait()
                return

        if self.behavior.should_end_session():
            self._end_of_session()
            self.behavior.end_session()
            self.behavior.idle_wait()
            return

        self._refresh_state()

        if not self.risk.can_trade():
            logger.info("Risk manager says NO. Waiting.")
            self.behavior.long_pause()
            return

        self.behavior.wait()

        # Clear external signal cache for new scan cycle
        self.external_signals.clear_cache()

        opportunities = self.scanner.scan()
        if not opportunities:
            logger.info("No opportunities found this scan.")
            self.behavior.wait()
            return

        # Filter to specialist categories
        specialist_opps = [
            opp for opp in opportunities
            if self._matches_specialist({
                "ticker": opp.ticker,
                "category": opp.category,
                "title": opp.title,
            })
        ]

        if not specialist_opps:
            logger.info("No specialist-matched opportunities for %s.", self.bot_name)
            self.behavior.wait()
            return

        logger.info(
            "%d specialist opportunities (from %d total).",
            len(specialist_opps), len(opportunities),
        )

        if self.behavior.should_browse_only():
            import random
            browse_target = random.choice(specialist_opps)
            self.scanner.enrich(browse_target)
            logger.info("Browsed %s without trading.", browse_target.ticker)
            self.behavior.record_action(traded=False)
            self.behavior.wait()
            return

        # Enrich top-N
        top_n = min(10, len(specialist_opps))
        for opp in specialist_opps[:top_n]:
            self.scanner.enrich(opp)
            self.behavior.wait()

        signals = self.analysis.analyse(specialist_opps[:top_n])
        if not signals:
            logger.info("No signals above confidence threshold.")
            self.behavior.record_action(traded=False)
            self.behavior.wait()
            return

        # --- Multi-signal execution ---
        # Execute all qualifying signals up to the risk manager's open position cap.
        # Each trade is checked individually against can_trade() to respect
        # daily limits, drawdown, and position count guards.
        max_signals_per_cycle = self.cfg.get("trading", {}).get("max_signals_per_cycle", 3)
        trades_executed = 0

        # Notify Telegram for each qualifying signal (pre-execution)
        for sig in signals[:max_signals_per_cycle]:
            self.notifier.notify_signal(
                ticker=sig.ticker,
                title=sig.title,
                side=sig.side,
                confidence=sig.confidence,
                price_cents=sig.suggested_price,
                bot_name=self.bot_name,
                rationale=sig.rationale,
            )

        for signal in signals[:max_signals_per_cycle]:
            if not self.risk.can_trade():
                logger.info("Risk manager halted further trades this cycle.")
                break
            # Skip if another bot already claimed this ticker
            # (conflict resolver is checked inside _execute_trade)
            self._execute_trade(signal)
            trades_executed += 1
            if trades_executed < len(signals[:max_signals_per_cycle]):
                self.behavior.wait()  # human-like gap between orders

        if trades_executed == 0:
            self.behavior.record_action(traded=False)

        self._reconcile_outcomes()

    def _execute_trade(self, signal) -> None:
        """Execute a trade -- mirrors agent.py logic."""
        base_count = self.risk.position_size(signal.confidence, signal.suggested_price)
        trend_mult = self.learning.trend.momentum_multiplier
        base_count = max(1, int(base_count * trend_mult))
        count = self.behavior.vary_trade_size(base_count)
        approval = self.central_llm.review_trade(
            bot_name=self.bot_name,
            trade_request={
                "ticker": signal.ticker,
                "title": signal.title,
                "category": signal.category,
                "side": signal.side,
                "action": signal.action,
                "quant_confidence": signal.confidence,
                "suggested_price": signal.suggested_price,
                "proposed_count": count,
                "event_ticker": signal.event_ticker,
            },
        )

        if approval.decision != "approve":
            logger.info(
                "Central LLM rejected trade %s %s on %s | rationale=%s | flags=%s",
                signal.action, signal.side, signal.ticker,
                approval.rationale, approval.red_flags,
            )
            self.behavior.record_action(traded=False)
            return

        count = max(1, int(count * approval.size_multiplier))
        signal.rationale = f"{signal.rationale} | CENTRAL_LLM: {approval.rationale}"

        logger.info(
            "Executing: %s %s on %s | conf=%.1f | price=%d | count=%d",
            signal.action, signal.side, signal.ticker,
            signal.confidence, signal.suggested_price, count,
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
            logger.info("Order placed: id=%s", order_id)

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
                bot_name=self.bot_name,
                order_id=order_id,
            )
            self.central_llm.record_execution(
                decision_id=approval.decision_id,
                order_id=order_id,
                trade_db_id=db_id,
            )
            self._pending_trades[int(db_id)] = {
                "ticker": signal.ticker,
                "order_id": str(order_id or ""),
                "count": int(count),
            }
            self._trade_count += 1
            self.behavior.record_action(traded=True)
            self.notifier.notify_trade(
                ticker=signal.ticker,
                side=signal.side,
                count=count,
                price_cents=signal.suggested_price,
                confidence=signal.confidence,
                order_id=order_id,
                bot_name=self.bot_name,
            )

        except KalshiAPIError as exc:
            logger.error("Order failed for %s: %s", signal.ticker, exc)
            self.behavior.record_action(traded=False)

    def _reconcile_outcomes(self) -> None:
        """
        Check for settled trades and update outcomes.

        v4 improvements:
        - Also checks positions directly for unrealized P&L on stale pending trades
          (trades open > stale_hours are force-resolved using current position data).
        - Triggers sell/exit orders for positions with significantly negative edge
          that have not yet settled naturally.
        """
        if not self._pending_trades:
            # Still check for stale DB entries (e.g. from a previous run)
            stale = self.learning.get_stale_pending_trades(
                min_age_hours=self.cfg.get("trading", {}).get("stale_trade_hours", 48.0)
            )
            for trade in stale:
                logger.warning(
                    "Found stale pending trade id=%d ticker=%s from %s. Force-resolving.",
                    trade["id"], trade["ticker"], trade["timestamp"],
                )
                self._force_resolve_trade(
                    trade["id"],
                    trade["ticker"],
                    trade.get("count", 1),
                    order_id=str(trade.get("order_id", "") or ""),
                )
            return

        try:
            positions = self.client.get_positions()
            pos_by_ticker = {p.get("ticker"): p for p in positions if p.get("ticker")}
            settlements = self.client.get_settlements()
            settlement_by_ticker = {
                str(s.get("ticker") or s.get("market_ticker") or "").strip(): s
                for s in settlements
                if str(s.get("ticker") or s.get("market_ticker") or "").strip()
            }
            settled_tickers = {
                s.get("ticker") or s.get("market_ticker")
                for s in settlements
                if s.get("ticker") or s.get("market_ticker")
            }

            resolved_ids: List[int] = []
            stale_age_hours = self.cfg.get("trading", {}).get("stale_trade_hours", 48.0)
            exit_threshold_cents = self.cfg.get("trading", {}).get("exit_loss_threshold_cents", -20)

            # Group by ticker to resolve duplicate-ticker pending rows safely.
            pending_by_ticker: Dict[str, List[tuple[int, Dict[str, Any]]]] = {}
            for db_id, meta in list(self._pending_trades.items()):
                ticker = str(meta.get("ticker", "")).strip()
                if not ticker:
                    continue
                pending_by_ticker.setdefault(ticker, []).append((db_id, meta))

            for ticker, rows in pending_by_ticker.items():
                # --- Case 1: Market has settled ---
                if ticker in settled_tickers:
                    settlement = settlement_by_ticker.get(ticker)
                    if settlement is not None:
                        total_pnl = self._settlement_pnl_cents(settlement)
                    else:
                        pos = pos_by_ticker.get(ticker, {})
                        total_pnl = int(pos.get("realized_pnl", 0) or 0)
                    allocations = self._allocate_pnl_across_rows(total_pnl, rows)
                    for db_id, meta, row_pnl in allocations:
                        outcome = self._outcome_from_pnl(row_pnl)
                        self.learning.update_outcome(db_id, outcome, pnl_cents=row_pnl)
                        self.risk.record_outcome(row_pnl)
                        self._on_trade_resolved(
                            outcome,
                            row_pnl,
                            ticker=ticker,
                            trade_db_id=db_id,
                            order_id=str(meta.get("order_id", "") or ""),
                        )
                        resolved_ids.append(db_id)
                    logger.info(
                        "Resolved %s across %d trade row(s): total %+d cents.",
                        ticker, len(rows), total_pnl,
                    )
                    continue

                # --- Case 2: Position exists but not settled yet ---
                pos = pos_by_ticker.get(ticker)
                if pos is not None:
                    unrealized = int(pos.get("unrealized_pnl", 0) or 0)
                    position_qty = int(pos.get("position", 0) or 0)

                    # Exit if unrealized loss exceeds threshold
                    if unrealized <= exit_threshold_cents and position_qty > 0:
                        logger.info(
                            "Exit signal for %s: unrealized P&L %+d¢ below threshold %+d¢. "
                            "Placing sell order.",
                            ticker, unrealized, exit_threshold_cents,
                        )
                        self._place_exit_order(ticker, pos)

                # --- Case 3: stale pending rows ---
                for db_id, meta in rows:
                    trade_ts_str = self._conn_get_trade_ts(db_id)
                    if not trade_ts_str:
                        continue
                    try:
                        trade_ts = datetime.fromisoformat(trade_ts_str)
                        age_hours = (datetime.now(timezone.utc) - trade_ts).total_seconds() / 3600.0
                        if age_hours > stale_age_hours:
                            logger.warning(
                                "Stale pending trade id=%d ticker=%s (%.1fh old). Force-resolving.",
                                db_id, ticker, age_hours,
                            )
                            self._force_resolve_trade(
                                db_id,
                                ticker,
                                int(meta.get("count", 1) or 1),
                                order_id=str(meta.get("order_id", "") or ""),
                            )
                            resolved_ids.append(db_id)
                    except Exception as exc:
                        logger.warning(
                            "Could not parse timestamp for trade id=%d ticker=%s: %s",
                            db_id, ticker, exc,
                        )

            for db_id in resolved_ids:
                self._pending_trades.pop(db_id, None)

        except Exception as exc:
            logger.warning("Reconciliation error: %s", exc)

    @staticmethod
    def _allocate_pnl_across_rows(
        total_pnl: int,
        rows: List[tuple[int, Dict[str, Any]]],
    ) -> List[tuple[int, Dict[str, Any], int]]:
        """Split ticker-level realized P&L across multiple pending trade rows."""
        if not rows:
            return []

        weights = [max(1, int(meta.get("count", 1) or 1)) for _, meta in rows]
        weight_sum = sum(weights)
        if weight_sum <= 0:
            per_row = int(total_pnl / len(rows))
            result = [(db_id, meta, per_row) for db_id, meta in rows]
            remainder = total_pnl - (per_row * len(rows))
            if result:
                db_id, meta, pnl = result[-1]
                result[-1] = (db_id, meta, pnl + remainder)
            return result

        allocated: List[tuple[int, Dict[str, Any], int]] = []
        running = 0
        for idx, (db_id, meta) in enumerate(rows):
            if idx == len(rows) - 1:
                row_pnl = total_pnl - running
            else:
                row_pnl = int(round(total_pnl * (weights[idx] / weight_sum)))
                running += row_pnl
            allocated.append((db_id, meta, row_pnl))
        return allocated

    @staticmethod
    def _outcome_from_pnl(pnl_cents: int) -> str:
        if pnl_cents > 0:
            return "win"
        if pnl_cents < 0:
            return "loss"
        return "breakeven"

    @staticmethod
    def _settlement_pnl_cents(settlement: Dict[str, Any]) -> int:
        """
        Compute realized P&L in cents from a settlement row.
        Uses revenue - total_cost - fee_cost.
        """
        try:
            revenue = int(settlement.get("revenue", 0) or 0)
        except Exception:
            revenue = 0
        try:
            yes_cost = int(settlement.get("yes_total_cost", 0) or 0)
        except Exception:
            yes_cost = 0
        try:
            no_cost = int(settlement.get("no_total_cost", 0) or 0)
        except Exception:
            no_cost = 0
        try:
            fee_cents = int(round(float(settlement.get("fee_cost", 0) or 0) * 100))
        except Exception:
            fee_cents = 0
        return int(revenue - yes_cost - no_cost - fee_cents)

    def _conn_get_trade_ts(self, trade_id: int) -> Optional[str]:
        """Helper: fetch timestamp for a trade by ID from the learning DB."""
        try:
            rows = self.learning._conn.execute(
                "SELECT timestamp FROM trades WHERE id = ?", (trade_id,)
            ).fetchone()
            return rows[0] if rows else None
        except Exception as exc:
            logger.debug("Could not fetch trade timestamp for id=%d: %s", trade_id, exc)
            return None

    def _force_resolve_trade(
        self,
        db_id: int,
        ticker: str,
        count: int,
        order_id: str = "",
    ) -> None:
        """
        Force-resolve a trade that is past its staleness threshold.
        Uses fill history to find the actual outcome; falls back to neutral.
        """
        try:
            fills = self.client.get_fills()
            fill_pnl = 0
            found = False
            for fill in fills:
                fill_ticker = str(fill.get("ticker") or fill.get("market_ticker") or "")
                if fill_ticker != ticker:
                    continue
                if order_id:
                    fill_order_candidates = {
                        str(fill.get("order_id", "") or ""),
                        str(fill.get("maker_order_id", "") or ""),
                        str(fill.get("taker_order_id", "") or ""),
                    }
                    if order_id not in fill_order_candidates:
                        continue
                try:
                    fill_pnl += int(fill.get("profit_loss", 0) or 0)
                except Exception:
                    fill_pnl += 0
                found = True

            if found:
                outcome = self._outcome_from_pnl(fill_pnl)
                self.learning.update_outcome(db_id, outcome, pnl_cents=fill_pnl)
                self.risk.record_outcome(fill_pnl)
                self._on_trade_resolved(
                    outcome,
                    fill_pnl,
                    ticker=ticker,
                    trade_db_id=db_id,
                    order_id=order_id,
                )
                logger.info("Force-resolved %s via fills: %s (%+d¢)", ticker, outcome, fill_pnl)
            else:
                # No fills found — mark as expired/neutral
                self.learning.update_outcome(db_id, "expired", pnl_cents=0)
                self._on_trade_resolved(
                    "expired",
                    0,
                    ticker=ticker,
                    trade_db_id=db_id,
                    order_id=order_id,
                )
                logger.info("Force-resolved %s as expired (no fills found).", ticker)
        except Exception as exc:
            logger.warning("Force-resolve failed for %s: %s", ticker, exc)

    def _on_trade_resolved(
        self,
        outcome: str,
        pnl_cents: int,
        ticker: str = "",
        trade_db_id: Optional[int] = None,
        order_id: str = "",
    ) -> None:
        """
        Called immediately after every trade outcome is recorded.

        Refreshes the learning engine's trend snapshot so that the next
        position-sizing call uses up-to-date momentum, and triggers a full
        weight recalibration whenever the review threshold is reached.

        This makes strategy adaptation happen per-resolution rather than
        waiting for a fixed batch boundary at the end of a trading cycle.
        """
        # Telegram outcome notification
        self.notifier.notify_outcome(
            ticker=ticker,
            outcome=outcome,
            pnl_cents=pnl_cents,
            bot_name=self.bot_name,
        )
        try:
            self.central_llm.record_trade_outcome(
                bot_name=self.bot_name,
                ticker=ticker,
                outcome=outcome,
                pnl_cents=pnl_cents,
                trade_db_id=trade_db_id,
                order_id=order_id,
            )
        except Exception as exc:
            logger.warning("Central LLM outcome feedback update failed: %s", exc)

        # Refresh trend (momentum_multiplier, hot/cold categories, calibration bias).
        # Cheap — one DB query over the last 20 settled trades.
        try:
            self.learning.compute_trend()
        except Exception as exc:
            logger.warning("Trend refresh failed after trade resolution: %s", exc)

        # Run full weight recalibration if the review threshold has been reached.
        if self.learning.should_review():
            try:
                new_weights = self.learning.review_and_recalibrate(self.analysis.weights)
                self.analysis.update_weights(new_weights)
                trend = self.learning.trend
                logger.info(
                    "Auto-recalibrated after %s resolution (pnl=%+d¢): "
                    "WR_delta=%.1f%% mult=%.2f hot=%s cold=%s weights=%s",
                    outcome, pnl_cents,
                    trend.win_rate_trend * 100,
                    trend.momentum_multiplier,
                    trend.hot_categories,
                    trend.cold_categories,
                    self.analysis.weights,
                )
            except Exception as exc:
                logger.warning("Weight recalibration failed after trade resolution: %s", exc)
        else:
            trend = self.learning.trend
            logger.debug(
                "Post-resolution trend: outcome=%s pnl=%+d¢ mult=%.2f bias=%.1f",
                outcome, pnl_cents,
                trend.momentum_multiplier,
                trend.calibration_bias,
            )

    def _place_exit_order(self, ticker: str, position: Dict[str, Any]) -> None:
        """
        Place a market sell order to exit a position with deteriorating edge.

        Kalshi doesn't support traditional stop-losses, so we sell our
        existing contracts at market price.
        """
        try:
            qty = abs(int(position.get("position", 0)))
            if qty == 0:
                return

            # Determine which side we hold
            side = position.get("side", "yes")

            result = self.client.create_order(
                ticker=ticker,
                side=side,
                action="sell",
                count=qty,
                order_type="market",
            )
            order = result.get("order", result)
            logger.info(
                "Exit order placed for %s: sell %d x %s | order_id=%s",
                ticker, qty, side, order.get("order_id", "unknown"),
            )
        except Exception as exc:
            logger.warning("Failed to place exit order for %s: %s", ticker, exc)

    def _refresh_state(self) -> None:
        """Refresh balance and position state."""
        try:
            logger.info(f"[{self.bot_name}] Fetching balance...")
            balance_data = self.client.get_balance()
            balance = balance_data.get("balance", 0)
            logger.info(f"[{self.bot_name}] Balance received: {balance}¢")
            
            # FIX: Only update if we got a valid balance, otherwise preserve last known
            if balance > 0:
                self._last_known_balance = balance
                self._last_balance_update = datetime.now(timezone.utc)
                self.risk.update_balance(balance)
            elif self._last_known_balance > 0:
                # API returned 0 but we have cached value - use cache
                logger.warning(f"[{self.bot_name}] API returned 0 balance, using cached {self._last_known_balance}¢")
                self.risk.update_balance(self._last_known_balance)
            else:
                # First run and API returned 0, update anyway
                self.risk.update_balance(balance)
                
        except KalshiAPIError as exc:
            logger.error(f"[{self.bot_name}] Failed to fetch balance: {exc}")
            # FIX: On API error, preserve last known balance
            if self._last_known_balance > 0:
                logger.info(f"[{self.bot_name}] Using cached balance {self._last_known_balance}¢")
                self.risk.update_balance(self._last_known_balance)
        except Exception as exc:
            logger.error(f"[{self.bot_name}] Unexpected error fetching balance: {exc}")
            # FIX: On any error, preserve last known balance
            if self._last_known_balance > 0:
                logger.info(f"[{self.bot_name}] Using cached balance {self._last_known_balance}¢")
                self.risk.update_balance(self._last_known_balance)

        try:
            positions = self.client.get_positions(count_filter="position")
            account_open_tickers = {
                str(p.get("ticker", "")).strip()
                for p in positions
                if str(p.get("ticker", "")).strip()
            }
            scope = str(
                self.cfg.get("risk", {}).get(
                    "open_positions_scope",
                    self.cfg.get("trading", {}).get("open_positions_scope", "per_bot"),
                )
            ).lower()
            if scope == "account":
                open_position_count = len(account_open_tickers)
            else:
                bot_open_tickers = {
                    str(meta.get("ticker", "")).strip()
                    for meta in self._pending_trades.values()
                    if str(meta.get("ticker", "")).strip()
                }
                open_position_count = len(account_open_tickers & bot_open_tickers)
            self.risk.update_open_positions(open_position_count)
        except KalshiAPIError as exc:
            logger.warning("Failed to fetch positions: %s", exc)

    def _end_of_session(self) -> None:
        """End-of-session tasks."""
        status = self.risk.status()
        perf = self.learning.get_performance(
            last_n=self.learning.cfg.get("rolling_window", 50)
        )
        self.learning.save_daily_summary(
            pnl_cents=status["daily_pnl_cents"],
            trades=status["trades_today"],
            wins=status["wins_today"],
            losses=status["losses_today"],
            avg_conf=perf.get("avg_confidence", 0),
        )
        self.notifier.notify_daily_summary(
            bot_name=self.bot_name,
            trades=status["trades_today"],
            wins=status["wins_today"],
            losses=status["losses_today"],
            pnl_cents=status["daily_pnl_cents"],
            win_rate=perf.get("win_rate", 0.0),
        )

    def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down bot '%s'.", self.bot_name)
        self._end_of_session()
        self._running = False
        self._set_status("stopped")
        self._save_risk_state()
        if self._status_thread and self._status_thread.is_alive():
            self._status_thread.join(timeout=2)
        self.learning.close()
        logger.info("Bot '%s' stopped.", self.bot_name)

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def _write_status(self, state: str, error: str = "") -> None:
        """Write status to a JSON file for the coordinator to read."""
        try:
            with self._status_lock:
                perf = self.learning.get_performance()
                risk_status = self.risk.status()

                # FIX: Ensure balance is never 0 in status if we have a cached value
                if risk_status.get("balance_cents", 0) == 0 and self._last_known_balance > 0:
                    risk_status["balance_cents"] = self._last_known_balance
                    risk_status["last_known_balance"] = True

                status = {
                    "bot_name": self.bot_name,
                    "display_name": self.cfg.get("bot", {}).get("display_name", self.bot_name),
                    "specialist": self.cfg.get("bot", {}).get("specialist", "general"),
                    "state": state,
                    "error": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "session_start": self._session_start.isoformat() if self._session_start else None,
                    "trade_count": self._trade_count,
                    "pending_trades": len(self._pending_trades),
                    "performance": perf,
                    "risk": risk_status,
                    "pid": os.getpid(),
                }

                # FIX: Use atomic write to prevent corruption
                temp_file = self._status_file.with_suffix('.tmp')
                with open(temp_file, "w") as fh:
                    json.dump(status, fh, indent=2)
                temp_file.replace(self._status_file)
                self._save_risk_state()

        except Exception as exc:
            logger.warning("Failed to write status file: %s", exc)

    # ------------------------------------------------------------------
    # External control
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Pause the bot (stop trading but keep running)."""
        self._paused = True
        logger.info("Bot '%s' paused.", self.bot_name)

    def resume(self) -> None:
        """Resume trading."""
        self._paused = False
        logger.info("Bot '%s' resumed.", self.bot_name)

    def stop(self) -> None:
        """Stop the bot."""
        self._running = False


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Bot Runner")
    parser.add_argument("--bot-name", required=True, help="Bot identifier")
    parser.add_argument(
        "--swarm-config",
        default="config/swarm_config.yaml",
        help="Path to swarm config",
    )
    parser.add_argument(
        "--bot-config",
        required=True,
        help="Path to bot-specific config",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root directory",
    )
    args = parser.parse_args()

    runner = BotRunner(
        bot_name=args.bot_name,
        swarm_config_path=args.swarm_config,
        bot_config_path=args.bot_config,
        project_root=args.project_root,
    )
    runner.run()


if __name__ == "__main__":
    main()

