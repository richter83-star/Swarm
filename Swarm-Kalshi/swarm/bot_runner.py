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
from kalshi_agent.mirofish_client import MiroFishClient
from telegram.notifier import TelegramNotifier
from swarm.balance_manager import BalanceManager
from swarm.central_llm_controller import CentralLLMController
from swarm.meta_learning import MetaLearner, SwarmMetaAggregator, CrossBotInsights
from swarm.rl_feedback import RLFeedbackBridge
from research.research_orchestrator import ResearchOrchestrator

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
        self._apply_autonomous_mode_overrides()
        self._setup_logging()
        self._running = True
        self._paused = False

        # Status file for coordinator communication
        self._status_file = self.project_root / "data" / f"{bot_name}_status.json"
        self._status_file.parent.mkdir(parents=True, exist_ok=True)
        self._trade_guard_snapshot_file = self.project_root / "data" / "swarm_trade_guard.json"
        self._trade_guard_max_age_seconds = int(
            self.cfg.get("swarm", {}).get("trade_guard_max_age_seconds", 45)
        )
        self._enforce_global_trade_guard = bool(
            self.cfg.get("swarm", {}).get("enforce_global_trade_guard", True)
        )
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

        self.mirofish = MiroFishClient(
            config=self.cfg.get("mirofish", {}),
        )

        self.analysis = AnalysisEngine(
            trading_cfg,
            weight_overrides=initial_weights,
            learning_engine=self.learning,
            external_signals=self.external_signals,
            llm_advisor=self.llm_advisor,
            research_config=self.cfg.get("research", {}),
            mirofish_client=self.mirofish,
        )

        self.behavior = HumanBehavior(
            self.cfg.get("human_behavior", {}),
            state_file=str(self.project_root / "data" / f"{bot_name}_behavior_state.json"),
        )
        self.risk = RiskManager(risk_cfg)
        self.balance_guard = BalanceManager(self.cfg.get("swarm", {}))
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

        # Centralized LLM controller (Anthropic) can approve/reject every trade.
        self.central_llm = CentralLLMController(
            config=self.cfg.get("central_llm", {}),
            project_root=str(self.project_root),
        )
        # Validate API key on startup — fail loudly rather than silently falling
        # back to quant scoring on every single trade without anyone noticing.
        self.central_llm.validate_api_key()

        # Research/evidence pipeline (degrades to {} on any failure).
        self.research = ResearchOrchestrator(config=self.cfg.get("research", {}))

        self._meta_learning_cfg = self.cfg.get("meta_learning", {}) or {}
        self.meta_learner: Optional[MetaLearner] = None
        self.rl_feedback = RLFeedbackBridge()

        if bool(self._meta_learning_cfg.get("enabled", False)):
            try:
                self.meta_learner = MetaLearner(
                    config=self._meta_learning_cfg,
                    project_root=str(self.project_root),
                    bot_name=self.bot_name,
                )
            except Exception as exc:
                logger.warning("MetaLearner init failed: %s", exc)

        # Swarm-wide meta insights (loaded fresh each scan cycle from coordinator output)
        self._swarm_insights: Optional[CrossBotInsights] = None
        self._swarm_insights_max_age = int(
            self._meta_learning_cfg.get("insights_max_age_seconds", 7200)
        )
        self._swarm_insights_file = str(
            self._meta_learning_cfg.get("insights_file", "data/swarm_meta_insights.json")
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
        self._routing_cfg = self._load_routing_config()
        self._unknown_category_policy = str(
            self._routing_cfg.get("unknown_category_policy", "probabilistic")
        ).strip().lower()
        self._series_prefix_category_map = {
            str(k).upper(): str(v).lower()
            for k, v in (self._routing_cfg.get("series_prefix_to_category", {}) or {}).items()
        }
        self._title_keyword_category_map = {
            str(k).lower(): str(v).lower()
            for k, v in (self._routing_cfg.get("title_keywords", {}) or {}).items()
        }
        self._event_pattern_category_map = {
            str(k).lower(): str(v).lower()
            for k, v in (self._routing_cfg.get("event_patterns", {}) or {}).items()
        }

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
                    "entry_price": int(row.get("entry_price", 0) or 0),
                    "title": str(row.get("title", "") or ""),
                    "category": str(row.get("category", "") or ""),
                    "confidence": float(row.get("confidence", 0.0) or 0.0),
                    "kelly_used": float(row.get("count", 1) or 1),
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

    def _apply_autonomous_mode_overrides(self) -> None:
        """
        Enforce single-switch autonomous behavior when configured.

        This keeps operation hands-off by ensuring the centralized LLM gate
        remains authoritative and optional local/manual assist paths are disabled.
        """
        auto_cfg = self.cfg.get("autonomous_mode", {}) or {}
        if not bool(auto_cfg.get("enabled", False)):
            return

        central_cfg = dict(self.cfg.get("central_llm", {}) or {})
        central_cfg["enabled"] = True
        if bool(auto_cfg.get("anthropic_only", True)):
            central_cfg["provider"] = "anthropic"
        if bool(auto_cfg.get("require_llm_for_trade", True)):
            central_cfg["allow_quant_fallback_on_error"] = False
            central_cfg["fail_open"] = False
        if bool(auto_cfg.get("llm_learning_enabled", True)):
            central_cfg["learning_enabled"] = True
        self.cfg["central_llm"] = central_cfg

        if bool(auto_cfg.get("disable_local_llm_advisor", True)):
            advisor_cfg = dict(self.cfg.get("llm_advisor", {}) or {})
            advisor_cfg["enabled"] = False
            self.cfg["llm_advisor"] = advisor_cfg

        logger.info(
            "Autonomous mode enabled: provider=%s require_llm_for_trade=%s "
            "disable_local_llm_advisor=%s",
            self.cfg.get("central_llm", {}).get("provider", "unknown"),
            bool(auto_cfg.get("require_llm_for_trade", True)),
            bool(auto_cfg.get("disable_local_llm_advisor", True)),
        )

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

    def _load_routing_config(self) -> Dict[str, Any]:
        """
        Load optional routing fallback config for missing category metadata.
        """
        defaults: Dict[str, Any] = {
            "unknown_category_policy": "probabilistic",
            "series_prefix_to_category": {
                "KXFED": "economics",
                "KXCPI": "economics",
                "KXBTCD": "economics",
                "KXETH": "economics",
                "KXTEMP": "weather",
                "KXRAIN": "weather",
                "KXSNOW": "weather",
                "KXHURR": "weather",
                "KXELECT": "politics",
                "KXPRES": "politics",
                "KXCONG": "politics",
            },
            "title_keywords": {
                "election": "politics",
                "president": "politics",
                "senate": "politics",
                "house": "politics",
                "inflation": "economics",
                "gdp": "economics",
                "unemployment": "economics",
                "fed": "economics",
                "bitcoin": "economics",
                "weather": "weather",
                "rain": "weather",
                "snow": "weather",
                "hurricane": "weather",
                "storm": "weather",
                "temperature": "weather",
                "climate": "weather",
                "science": "weather",
            },
            "event_patterns": {
                "elect": "politics",
                "pres": "politics",
                "cong": "politics",
                "cpi": "economics",
                "fed": "economics",
                "gdp": "economics",
                "unemp": "economics",
                "btc": "economics",
                "temp": "weather",
                "rain": "weather",
                "snow": "weather",
                "hurr": "weather",
                "climate": "weather",
            },
        }
        routing_path = self.project_root / "config" / "routing_config.yaml"
        try:
            if routing_path.exists():
                with open(routing_path, "r", encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                if isinstance(loaded, dict):
                    return self._deep_merge(defaults, loaded)
        except Exception as exc:
            logger.warning("Failed loading routing config %s: %s", routing_path, exc)
        return defaults

    def _infer_market_category(self, market: Dict[str, Any]) -> tuple[str, str, float]:
        """
        Infer a category when API payloads omit explicit category metadata.
        Returns (category, source, confidence).
        """
        category = str(market.get("category", "") or "").strip().lower()
        if category:
            return category, "category", 1.0

        ticker = str(market.get("ticker", "") or "").strip().upper()
        event_ticker = str(market.get("event_ticker", "") or "").strip().lower()
        title = str(market.get("title", "") or "").strip().lower()

        for prefix, mapped_category in self._series_prefix_category_map.items():
            if ticker.startswith(prefix):
                return mapped_category, "series_prefix", 0.8

        for kw, mapped_category in self._title_keyword_category_map.items():
            if kw and kw in title:
                return mapped_category, "title_keyword", 0.7

        for pattern, mapped_category in self._event_pattern_category_map.items():
            if pattern and pattern in event_ticker:
                return mapped_category, "event_pattern", 0.6

        return "", "unknown", 0.0

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
        inferred_category, source, confidence = self._infer_market_category(market)

        # If no filters, accept everything (Vanguard behavior)
        if not self._category_filters and not self._series_filters:
            # But check exclusions
            if inferred_category in self._excluded_categories:
                return False
            return True

        ticker = market.get("ticker", "")
        category = inferred_category
        title = (market.get("title") or "").lower()
        series_ticker = ticker.split("-")[0].upper() if "-" in ticker else ticker.upper()

        # Series match
        if self._series_filters and series_ticker in self._series_filters:
            return True

        # Category match
        if category and category in self._category_filters:
            return True

        # Partial category match
        if category:
            for cat_filter in self._category_filters:
                if cat_filter in category or category in cat_filter:
                    return True

        # Keyword match
        if self._category_keywords:
            for kw in self._category_keywords:
                if kw in title:
                    return True

        if not category and self._unknown_category_policy == "strict":
            logger.debug(
                "Dropped unknown-category market for %s under strict policy: %s",
                self.bot_name, ticker,
            )
            return False

        logger.debug(
            "No specialist match for %s (bot=%s category_source=%s confidence=%.2f inferred_category=%s).",
            ticker, self.bot_name, source, confidence, category or "unknown",
        )
        return False

    def _should_run_weekly_backtest(self) -> bool:
        """Return True if it's been 7+ days since the last backtest run."""
        if self._last_backtest_date is None:
            return False
        from datetime import timedelta
        return (datetime.now(timezone.utc) - self._last_backtest_date) >= timedelta(days=7)

    @staticmethod
    def _normalize_meta_domain(category: str) -> str:
        allowed = {"sports", "politics", "crypto", "economics", "weather", "entertainment"}
        value = str(category or "").strip().lower()
        if value in allowed:
            return value
        alias_map = {
            "finance": "economics",
            "business": "economics",
            "elections": "politics",
            "government": "politics",
            "climate": "weather",
            "science": "weather",
            "movies": "entertainment",
            "tv": "entertainment",
        }
        for src, dst in alias_map.items():
            if src in value:
                return dst
        return "entertainment"

    def _apply_meta_strategy_before_analysis(self, opportunities: List[Any]) -> None:
        if self.meta_learner is None:
            return
        if not opportunities:
            return

        try:
            if self.meta_learner.task_count() < 10:
                return

            target = opportunities[0]
            task_description = str(getattr(target, "title", "") or "")
            domain = self._normalize_meta_domain(str(getattr(target, "category", "") or ""))

            strategy, confidence, hyperparams = self.meta_learner.predict_strategy(
                task_description=task_description,
                domain=domain,
            )

            min_conf = float(self._meta_learning_cfg.get("min_confidence_to_apply", 0.7) or 0.7)
            if confidence <= min_conf:
                return

            if "temperature" not in hyperparams:
                return

            old_temp = float(self.cfg.get("llm_advisor", {}).get("temperature", 0.1) or 0.1)
            new_temp = round(max(0.0, min(1.0, float(hyperparams["temperature"]))), 3)
            if abs(new_temp - old_temp) < 1e-9:
                return

            self.cfg.setdefault("llm_advisor", {})["temperature"] = new_temp
            if hasattr(self.llm_advisor, "cfg") and isinstance(self.llm_advisor.cfg, dict):
                self.llm_advisor.cfg["temperature"] = new_temp

            reason = (
                f"meta_predict_strategy strategy={strategy} domain={domain} confidence={confidence:.3f}"
            )
            self.meta_learner.log_config_mutation(
                bot_name=self.bot_name,
                config_key="llm_advisor.temperature",
                old_value=old_temp,
                new_value=new_temp,
                reason=reason,
                source="meta_learning",
            )
            logger.info(
                "MetaLearner applied temperature mutation for %s: %.3f -> %.3f (%s)",
                self.bot_name,
                old_temp,
                new_temp,
                reason,
            )
        except Exception as exc:
            # Never block trade flow.
            logger.warning("MetaLearner pre-analysis hook failed: %s", exc)

    def _refresh_swarm_insights(self) -> None:
        """Reload cross-bot meta insights from the coordinator's JSON file."""
        try:
            self._swarm_insights = SwarmMetaAggregator.load_insights(
                project_root=str(self.project_root),
                max_age_seconds=self._swarm_insights_max_age,
                insights_file=self._swarm_insights_file,
            )
        except Exception as exc:
            logger.debug("Swarm insights load failed (non-fatal): %s", exc)
            self._swarm_insights = None

    def _apply_swarm_multiplier_to_signals(
        self, signals: List[Any]
    ) -> List[Any]:
        """
        Blend swarm-wide category edge into each signal's confidence score.

        For each signal the final confidence is:
            confidence = own_confidence * blended_multiplier

        where ``blended_multiplier`` is the weighted average of the bot's own
        LearningEngine category multiplier (weight depends on own data density)
        and the swarm-wide multiplier derived from all bots' settled trades.

        This is intentionally capped and conservative:
        - Requires ``min_trades_threshold`` settled swarm trades for the category.
        - Max multiplier shift: ±30% (same as own LearningEngine cap).
        - If no swarm data is available the signals pass through unchanged.
        """
        if not signals or self._swarm_insights is None:
            return signals

        adjusted: List[Any] = []
        for sig in signals:
            try:
                category = str(getattr(sig, "category", "") or "").lower()

                # Own LearningEngine multiplier for this category
                own_trades = 0
                own_mult = 1.0
                if self.learning is not None:
                    row = self.learning._conn.execute(
                        "SELECT trades, wins FROM category_stats WHERE category = ?",
                        (category,),
                    ).fetchone()
                    if row:
                        own_trades = int(row["trades"] or 0)
                        if own_trades >= 5:
                            own_mult = self.learning.get_category_multiplier(category)

                if self.meta_learner is not None:
                    blended = self.meta_learner.get_swarm_category_multiplier(
                        category=category,
                        own_multiplier=own_mult,
                        insights=self._swarm_insights,
                        own_trades=own_trades,
                    )
                else:
                    swarm_mult = self._swarm_insights.get_category_multiplier(category)
                    blended = swarm_mult if swarm_mult is not None else own_mult

                if abs(blended - 1.0) < 1e-6:
                    adjusted.append(sig)
                    continue

                old_conf = sig.confidence
                sig.confidence = round(max(0.0, min(100.0, sig.confidence * blended)), 2)

                if abs(sig.confidence - old_conf) > 0.5:
                    logger.debug(
                        "Swarm meta-adjust %s [%s]: conf %.1f→%.1f (mult=%.3f "
                        "hot=%s cold=%s own_trades=%d)",
                        sig.ticker, category,
                        old_conf, sig.confidence, blended,
                        category in self._swarm_insights.hot_categories,
                        category in self._swarm_insights.cold_categories,
                        own_trades,
                    )

            except Exception as exc:
                logger.debug("Swarm multiplier failed for signal: %s", exc)

            adjusted.append(sig)

        return adjusted

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
                self._check_coordinator_signals()

                if self._paused:
                    # Auto-resume check
                    try:
                        if self._risk_state_file.exists():
                            with open(self._risk_state_file, "r", encoding="utf-8") as fh:
                                risk_state = json.load(fh)
                            if risk_state.get("auto_paused"):
                                resume_at_str = risk_state.get("auto_resume_at")
                                if resume_at_str:
                                    resume_at = datetime.fromisoformat(resume_at_str)
                                    if datetime.now(timezone.utc) >= resume_at:
                                        self._paused = False
                                        risk_state.pop("auto_paused", None)
                                        risk_state.pop("auto_pause_reason", None)
                                        risk_state.pop("auto_pause_at", None)
                                        risk_state.pop("auto_resume_at", None)
                                        temp_file = self._risk_state_file.with_suffix(".tmp")
                                        with open(temp_file, "w", encoding="utf-8") as fh:
                                            json.dump(risk_state, fh, indent=2)
                                        temp_file.replace(self._risk_state_file)
                                        logger.info(
                                            "Bot '%s' auto-resumed after cooldown.", self.bot_name
                                        )
                                        try:
                                            self.notifier.notify_crash(
                                                bot_name=self.bot_name,
                                                error=f"Auto-resumed after 24h cooldown (consecutive loss pause lifted).",
                                            )
                                        except Exception:
                                            pass
                    except Exception as exc:
                        logger.warning("Auto-resume check failed: %s", exc)

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

        # Filter to specialist categories; also backfill inferred category onto
        # each opportunity so analysis engine and meta-learner see it (the raw
        # Kalshi API payload often omits the `category` field entirely).
        specialist_opps = []
        for opp in opportunities:
            market_dict = {
                "ticker": opp.ticker,
                "category": opp.category,
                "title": opp.title,
                "event_ticker": opp.event_ticker,
            }
            inferred_cat, _src, _conf = self._infer_market_category(market_dict)
            if not opp.category and inferred_cat:
                opp.category = inferred_cat
            if self._matches_specialist(market_dict | {"category": opp.category}):
                specialist_opps.append(opp)

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

        self._refresh_swarm_insights()
        self._apply_meta_strategy_before_analysis(specialist_opps[:top_n])

        signals = self.analysis.analyse(specialist_opps[:top_n])
        signals = self._apply_swarm_multiplier_to_signals(signals)
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

    def _apply_final_position_cap(
        self,
        ticker: str,
        price_cents: int,
        requested_count: int,
        base_count: int,
        trend_mult: float,
        human_mult: float,
        llm_mult: float,
    ) -> int:
        """
        Enforce a hard notional cap *after* all multipliers.
        """
        if price_cents <= 0:
            return 0
        if requested_count <= 0:
            return 0

        trading_cfg = self.cfg.get("trading", {}) or {}
        max_pct = float(trading_cfg.get("max_position_pct", 0.05) or 0.0)
        state = self.risk.export_state()
        balance_cents = int(state.get("current_balance_cents", 0) or 0)
        hard_cap_cents = int(max(0, balance_cents) * max(0.0, max_pct))
        requested_notional = int(requested_count * price_cents)

        if hard_cap_cents <= 0:
            logger.warning(
                "Hard cap rejected %s: balance=%d cap_pct=%.4f cap_cents=%d",
                ticker, balance_cents, max_pct, hard_cap_cents,
            )
            return 0

        capped_count = min(requested_count, int(hard_cap_cents // price_cents))
        final_notional = int(capped_count * price_cents)
        logger.info(
            "Trade sizing %s | base_size_cents=%d trend_mult=%.3f human_mult=%.3f "
            "llm_mult=%.3f requested_size_cents=%d hard_cap_cents=%d final_capped_size_cents=%d",
            ticker,
            int(base_count * price_cents),
            float(trend_mult),
            float(human_mult),
            float(llm_mult),
            requested_notional,
            hard_cap_cents,
            final_notional,
        )
        return max(0, capped_count)

    def _authorize_global_trade(
        self,
        ticker: str,
        notional_cents: int,
    ) -> tuple[bool, str]:
        """
        Fail-safe pre-trade gate using coordinator-published trade guard snapshot.
        """
        if not self._enforce_global_trade_guard:
            return True, "global_trade_guard_disabled"

        try:
            snapshot, reason = BalanceManager.load_trade_guard_snapshot(
                str(self._trade_guard_snapshot_file),
                max_age_seconds=self._trade_guard_max_age_seconds,
            )
            if snapshot is None:
                return False, reason
            return self.balance_guard.can_execute_trade(
                bot_name=self.bot_name,
                ticker=ticker,
                notional_cents=notional_cents,
                guard_snapshot=snapshot,
            )
        except Exception as exc:
            return False, f"trade_guard_exception:{exc}"

    def _execute_trade(self, signal) -> None:
        """Execute a trade -- mirrors agent.py logic."""
        base_count = self.risk.position_size(signal.confidence, signal.suggested_price)
        trend_mult = float(self.learning.trend.momentum_multiplier)
        trend_count = max(1, int(base_count * trend_mult))
        pre_human_count = trend_count
        count = self.behavior.vary_trade_size(pre_human_count)
        human_mult = float(count / max(1, pre_human_count))
        # Pre-screen locally before burning Tavily (research) + Anthropic (LLM) credits.
        # Rejects candidates whose quant confidence falls below the archetype-aware floor
        # (low volume, wide spread, longshot price) — these would be auto-rejected after
        # the API call anyway, so skip both external calls entirely.
        if not self.central_llm.pre_screen({
            "ticker": signal.ticker,
            "quant_confidence": signal.confidence,
            "volume_24h": int(getattr(signal, "volume_24h", 0) or 0),
            "spread_cents": int(getattr(signal, "spread_cents", 0) or 0),
            "suggested_price": signal.suggested_price,
        }):
            logger.info(
                "Pre-screen rejected %s %s on %s (conf=%.1f below archetype floor)"
                " — skipped research + LLM to save API credits.",
                signal.action, signal.side, signal.ticker, signal.confidence,
            )
            self.behavior.record_action(traded=False)
            return

        # Enrich with web research evidence (returns {} gracefully on any failure).
        research_data = self.research.enrich_trade_request(signal)

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
                "volume_24h": int(getattr(signal, "volume_24h", 0) or 0),
                "spread_cents": int(getattr(signal, "spread_cents", 0) or 0),
                # Research enrichment fields (empty when research is disabled/failed)
                "research_summary": research_data.get("research_summary", ""),
                "evidence_quality": research_data.get("evidence_quality", None),
                "evidence_bullets": research_data.get("evidence_bullets", []),
                "num_sources": research_data.get("num_sources", 0),
                "evidence_contradictions": research_data.get("evidence_contradictions", []),
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

        llm_mult = float(approval.size_multiplier)
        requested_count = max(1, int(count * llm_mult))
        count = self._apply_final_position_cap(
            ticker=signal.ticker,
            price_cents=int(signal.suggested_price),
            requested_count=requested_count,
            base_count=base_count,
            trend_mult=trend_mult,
            human_mult=human_mult,
            llm_mult=llm_mult,
        )
        if count <= 0:
            logger.warning(
                "Trade rejected by hard cap: %s %s on %s",
                signal.action, signal.side, signal.ticker,
            )
            self.behavior.record_action(traded=False)
            return

        notional_cents = int(count * int(signal.suggested_price))
        auth_ok, auth_reason = self._authorize_global_trade(signal.ticker, notional_cents)
        if not auth_ok:
            logger.warning(
                "Trade rejected by global guard: %s %s on %s | reason=%s",
                signal.action, signal.side, signal.ticker, auth_reason,
            )
            self.behavior.record_action(traded=False)
            return

        route_category, route_source, route_conf = self._infer_market_category(
            {
                "ticker": signal.ticker,
                "category": signal.category,
                "title": signal.title,
                "event_ticker": signal.event_ticker,
            }
        )
        # Backfill inferred category onto signal so it's stored in the trade DB.
        # The raw API often omits `category`; without this the meta-learner sees
        # only one "unknown" bucket and can't build per-category multipliers.
        if not signal.category and route_category:
            signal.category = route_category

        signal.rationale = (
            f"{signal.rationale} | ROUTE:source={route_source} "
            f"assigned={route_category or 'unknown'} conf={route_conf:.2f} "
            f"| CENTRAL_LLM: {approval.rationale}"
        )

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
                "entry_price": int(signal.suggested_price),
                "title": str(signal.title or ""),
                "category": str(signal.category or ""),
                "confidence": float(signal.confidence or 0.0),
                "kelly_used": float(base_count),
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
            fills: List[Dict[str, Any]] = []
            try:
                fills = self.client.get_fills(limit=500)
            except Exception as exc:
                logger.warning("Could not fetch fills for reconciliation: %s", exc)
            settlement_by_ticker = {
                str(s.get("ticker") or s.get("market_ticker") or "").strip(): s
                for s in settlements
                if str(s.get("ticker") or s.get("market_ticker") or "").strip()
            }
            fills_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
            for fill in fills:
                fill_ticker = str(fill.get("ticker") or fill.get("market_ticker") or "").strip()
                if fill_ticker:
                    fills_by_ticker.setdefault(fill_ticker, []).append(fill)
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
                    ticker_fills = fills_by_ticker.get(ticker, [])
                    allocations_with_source: List[tuple[int, Dict[str, Any], int, str]] = []
                    attributed_total = 0
                    unattributed_rows: List[tuple[int, Dict[str, Any]]] = []

                    # Prefer exact order-level attribution from fills.
                    for db_id, meta in rows:
                        order_id = str(meta.get("order_id", "") or "")
                        found_fill_pnl, fill_pnl = self._sum_fill_pnl_for_order(
                            ticker_fills, order_id
                        )
                        if found_fill_pnl:
                            allocations_with_source.append((db_id, meta, fill_pnl, "fills_by_order"))
                            attributed_total += fill_pnl
                        else:
                            # Log when fills exist for this order but profit_loss is null
                            # (Kalshi API omits profit_loss until settlement; use settlement total instead)
                            has_any_fill = any(
                                order_id in self._fill_order_candidates(f)
                                for f in ticker_fills
                            )
                            if has_any_fill:
                                logger.debug(
                                    "Fills found for order %s (%s) but profit_loss is null; "
                                    "falling back to settlement allocation (total=%+d¢).",
                                    order_id, ticker, total_pnl,
                                )
                            unattributed_rows.append((db_id, meta))

                    if unattributed_rows:
                        residual_total = int(total_pnl - attributed_total)
                        min_allowed_total, max_allowed_total = self._aggregate_allowed_pnl_bounds(
                            unattributed_rows
                        )
                        residual_in_bounds = (
                            min_allowed_total <= residual_total <= max_allowed_total
                        )

                        if not residual_in_bounds:
                            # Settlement ticker-level totals can include unrelated historical rows.
                            # Fail safe by excluding ambiguous rows from learning updates.
                            logger.warning(
                                "Ambiguous settlement allocation for %s: residual=%+d outside "
                                "[%+d, %+d] for %d row(s). Marking unresolved rows as expired.",
                                ticker,
                                residual_total,
                                min_allowed_total,
                                max_allowed_total,
                                len(unattributed_rows),
                            )
                            for db_id, meta in unattributed_rows:
                                allocations_with_source.append(
                                    (db_id, meta, 0, "settlement_ambiguous_expired")
                                )
                        elif len(unattributed_rows) == 1:
                            db_id, meta = unattributed_rows[0]
                            allocations_with_source.append(
                                (db_id, meta, residual_total, "settlement_residual_single")
                            )
                        else:
                            for db_id, meta, row_pnl in self._allocate_pnl_across_rows(
                                residual_total, unattributed_rows
                            ):
                                allocations_with_source.append(
                                    (db_id, meta, row_pnl, "settlement_weighted")
                                )

                    for db_id, meta, row_pnl, allocation_source in allocations_with_source:
                        if allocation_source == "settlement_ambiguous_expired":
                            outcome = "expired"
                            self.learning.update_outcome(
                                db_id,
                                outcome,
                                pnl_cents=0,
                                pnl_valid=True,
                                pnl_validation_reason="ambiguous_settlement_total",
                                reconciliation_trace={
                                    "ticker": ticker,
                                    "source": allocation_source,
                                    "total_ticker_pnl_cents": int(total_pnl),
                                    "attributed_fill_pnl_cents": int(attributed_total),
                                },
                            )
                            self._on_trade_resolved(
                                outcome,
                                0,
                                ticker=ticker,
                                trade_db_id=db_id,
                                order_id=str(meta.get("order_id", "") or ""),
                            )
                            resolved_ids.append(db_id)
                            continue

                        pnl_ok, pnl_reason, pnl_trace = self._validate_resolved_trade_pnl(
                            row_meta=meta,
                            pnl_cents=row_pnl,
                        )
                        if pnl_ok:
                            outcome = self._outcome_from_pnl(row_pnl)
                            self.learning.update_outcome(
                                db_id,
                                outcome,
                                pnl_cents=row_pnl,
                                pnl_valid=True,
                                pnl_validation_reason="ok",
                                reconciliation_trace={
                                    **pnl_trace,
                                    "ticker": ticker,
                                    "source": allocation_source,
                                    "total_ticker_pnl_cents": int(total_pnl),
                                    "attributed_fill_pnl_cents": int(attributed_total),
                                },
                            )
                            self.risk.record_outcome(row_pnl)
                        else:
                            outcome = "pnl_invalid"
                            # P0-3 fix: clamp the recorded value to the theoretical max loss
                            # so that downstream learning/autoscale never sees an impossible loss.
                            count_meta = max(1, int(meta.get("count", 1) or 1))
                            entry_meta = max(0, int(meta.get("entry_price", 0) or 0))
                            max_theoretical_loss = -(count_meta * entry_meta)
                            clamped_pnl = max(row_pnl, max_theoretical_loss)
                            logger.warning(
                                "P&L invariant failed for %s trade_id=%d: %s "
                                "raw_pnl=%+d clamped_pnl=%+d trace=%s",
                                ticker, db_id, pnl_reason,
                                row_pnl, clamped_pnl, pnl_trace,
                            )
                            self.learning.update_outcome(
                                db_id,
                                outcome,
                                pnl_cents=clamped_pnl,
                                pnl_valid=False,
                                pnl_validation_reason=pnl_reason,
                                reconciliation_trace={
                                    **pnl_trace,
                                    "ticker": ticker,
                                    "source": allocation_source,
                                    "total_ticker_pnl_cents": int(total_pnl),
                                    "attributed_fill_pnl_cents": int(attributed_total),
                                    "raw_pnl_cents": int(row_pnl),
                                },
                            )
                            # Do NOT feed pnl_invalid row into risk state — use 0 so the
                            # risk manager, LLM controller and meta-RL are not poisoned.
                            row_pnl = 0
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
    def _fill_order_candidates(fill: Dict[str, Any]) -> set[str]:
        return {
            str(fill.get("order_id", "") or ""),
            str(fill.get("maker_order_id", "") or ""),
            str(fill.get("taker_order_id", "") or ""),
        }

    @classmethod
    def _sum_fill_pnl_for_order(
        cls,
        fills_for_ticker: List[Dict[str, Any]],
        order_id: str,
    ) -> tuple[bool, int]:
        normalized_order_id = str(order_id or "")
        if not normalized_order_id:
            return False, 0
        total = 0
        found = False       # True if any fill matched this order_id
        has_pnl_data = False  # True if at least one fill had a non-null profit_loss field
        for fill in fills_for_ticker:
            if normalized_order_id not in cls._fill_order_candidates(fill):
                continue
            found = True
            if fill.get("profit_loss") is not None:
                has_pnl_data = True
                try:
                    total += int(fill["profit_loss"])
                except Exception:
                    pass
        # Only report as attributed if profit_loss data was actually present.
        # If fills exist but all have null profit_loss, fall through to settlement
        # allocation so the correct settlement-level P&L is used instead of 0.
        return (found and has_pnl_data), int(total)

    @classmethod
    def _aggregate_allowed_pnl_bounds(
        cls,
        rows: List[tuple[int, Dict[str, Any]]],
    ) -> tuple[int, int]:
        min_total = 0
        max_total = 0
        for _, meta in rows:
            _, _, trace = cls._validate_resolved_trade_pnl(meta, pnl_cents=0)
            min_total += int(trace.get("min_allowed_cents", 0) or 0)
            max_total += int(trace.get("max_allowed_cents", 0) or 0)
        return min_total, max_total

    @staticmethod
    def _validate_resolved_trade_pnl(
        row_meta: Dict[str, Any],
        pnl_cents: int,
        tolerance_pct: float = 0.05,
    ) -> tuple[bool, str, Dict[str, Any]]:
        """
        Validate realized P&L against theoretical bounds for one trade row.
        """
        count = max(1, int(row_meta.get("count", 1) or 1))
        entry_price = int(row_meta.get("entry_price", 0) or 0)
        max_loss = max(0, count * max(0, entry_price))
        max_gain = max(0, count * max(0, 100 - entry_price))
        tol = int(max(max_loss, max_gain) * max(0.0, float(tolerance_pct)))
        tol = max(tol, 5)  # Avoid zero-tolerance false positives on tiny positions.
        min_allowed = -max_loss - tol
        max_allowed = max_gain + tol

        trace = {
            "count": count,
            "entry_price_cents": entry_price,
            "pnl_cents": int(pnl_cents),
            "max_theoretical_loss_cents": -max_loss,
            "max_theoretical_gain_cents": max_gain,
            "tolerance_cents": tol,
            "min_allowed_cents": min_allowed,
            "max_allowed_cents": max_allowed,
        }

        if pnl_cents < min_allowed:
            return False, f"loss exceeds theoretical bound ({pnl_cents} < {min_allowed})", trace
        if pnl_cents > max_allowed:
            return False, f"gain exceeds theoretical bound ({pnl_cents} > {max_allowed})", trace
        return True, "ok", trace

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
            # Kalshi settlement monetary fields are typically cents in this API.
            # Keep fee in the same unit family as revenue/cost values.
            fee_cents = int(round(float(settlement.get("fee_cost", 0) or 0)))
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
                    fill_order_candidates = self._fill_order_candidates(fill)
                    if order_id not in fill_order_candidates:
                        continue
                try:
                    fill_pnl += int(fill.get("profit_loss", 0) or 0)
                except Exception:
                    fill_pnl += 0
                found = True

            if found:
                # P0-3 fix: validate fill P&L against theoretical bounds before recording.
                row_meta_for_validation = self._pending_trades.get(int(db_id), {})
                if not row_meta_for_validation:
                    # Build a minimal meta dict from the known count/entry if available.
                    row_meta_for_validation = {"count": count, "entry_price": 0}
                pnl_ok, pnl_reason, pnl_trace = self._validate_resolved_trade_pnl(
                    row_meta=row_meta_for_validation,
                    pnl_cents=fill_pnl,
                )
                if not pnl_ok:
                    count_fr = max(1, int(row_meta_for_validation.get("count", count) or count))
                    entry_fr = max(0, int(row_meta_for_validation.get("entry_price", 0) or 0))
                    max_theoretical_loss = -(count_fr * entry_fr)
                    raw_fill_pnl = fill_pnl
                    fill_pnl = max(fill_pnl, max_theoretical_loss)
                    logger.warning(
                        "Force-resolve P&L invariant failed for %s db_id=%d: %s "
                        "raw_pnl=%+d clamped_pnl=%+d trace=%s",
                        ticker, db_id, pnl_reason, raw_fill_pnl, fill_pnl, pnl_trace,
                    )
                    outcome = "pnl_invalid"
                    self.learning.update_outcome(
                        db_id,
                        outcome,
                        pnl_cents=fill_pnl,
                        pnl_valid=False,
                        pnl_validation_reason=pnl_reason,
                        reconciliation_trace={
                            "source": "force_resolve_fills",
                            "ticker": ticker,
                            "order_id": order_id,
                            "raw_pnl_cents": int(raw_fill_pnl),
                            **pnl_trace,
                        },
                    )
                    # Do NOT feed pnl_invalid row into risk state — pass 0 downstream.
                    self._on_trade_resolved(
                        outcome,
                        0,
                        ticker=ticker,
                        trade_db_id=db_id,
                        order_id=order_id,
                    )
                    logger.info(
                        "Force-resolved %s as pnl_invalid (raw %+d¢ clamped %+d¢).",
                        ticker, raw_fill_pnl, fill_pnl,
                    )
                else:
                    outcome = self._outcome_from_pnl(fill_pnl)
                    self.learning.update_outcome(
                        db_id,
                        outcome,
                        pnl_cents=fill_pnl,
                        pnl_valid=True,
                        pnl_validation_reason="ok",
                        reconciliation_trace={
                            "source": "force_resolve_fills",
                            "ticker": ticker,
                            "order_id": order_id,
                        },
                    )
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
                self.learning.update_outcome(
                    db_id,
                    "expired",
                    pnl_cents=0,
                    pnl_valid=True,
                    pnl_validation_reason="no_fills_expired",
                    reconciliation_trace={
                        "source": "force_resolve_fills",
                        "ticker": ticker,
                        "order_id": order_id,
                    },
                )
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

        # Auto-pause on consecutive losses
        try:
            auto_pause_threshold = int(
                self.cfg.get("trading", {}).get("auto_pause_on_consecutive_losses", 8)
            )
            resume_hours = float(
                self.cfg.get("trading", {}).get("auto_pause_resume_hours", 24)
            )
            consecutive = int(getattr(self.risk, "_consecutive_losses", 0) or 0)
            if consecutive >= auto_pause_threshold and not self._paused:
                self._paused = True
                now_utc = datetime.now(timezone.utc)
                from datetime import timedelta
                resume_at = now_utc + timedelta(hours=resume_hours)
                pause_reason = (
                    f"Auto-paused after {consecutive} consecutive losses "
                    f"(threshold={auto_pause_threshold})"
                )
                logger.warning(
                    "Bot '%s' %s. Auto-resume at %s.",
                    self.bot_name, pause_reason, resume_at.isoformat(),
                )
                # Persist auto-pause info to risk state file
                try:
                    risk_state: Dict[str, Any] = {}
                    if self._risk_state_file.exists():
                        with open(self._risk_state_file, "r", encoding="utf-8") as fh:
                            risk_state = json.load(fh)
                    risk_state["auto_paused"] = True
                    risk_state["auto_pause_reason"] = pause_reason
                    risk_state["auto_pause_at"] = now_utc.isoformat()
                    risk_state["auto_resume_at"] = resume_at.isoformat()
                    temp_file = self._risk_state_file.with_suffix(".tmp")
                    with open(temp_file, "w", encoding="utf-8") as fh:
                        json.dump(risk_state, fh, indent=2)
                    temp_file.replace(self._risk_state_file)
                except Exception as exc:
                    logger.warning("Could not persist auto-pause state: %s", exc)
                try:
                    self.notifier.notify_crash(
                        bot_name=self.bot_name,
                        error=pause_reason,
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Auto-pause check failed: %s", exc)
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
        try:
            if self.meta_learner is not None and trade_db_id is not None:
                meta = self._pending_trades.get(int(trade_db_id), {})
                self.rl_feedback.record_outcome(
                    meta_learner=self.meta_learner,
                    bot_name=self.bot_name,
                    ticker=ticker,
                    market_title=str(meta.get("title", "") or ticker),
                    market_category=self._normalize_meta_domain(str(meta.get("category", "") or "")),
                    confidence_at_entry=float(meta.get("confidence", 0.0) or 0.0),
                    outcome=outcome,
                    pnl_cents=int(pnl_cents),
                    kelly_used=float(meta.get("kelly_used", meta.get("count", 0.0)) or 0.0),
                )
        except Exception as exc:
            # Never block reconciliation.
            logger.warning("Meta RL feedback hook failed: %s", exc)

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

    def _check_coordinator_signals(self) -> None:
        """Check for coordinator signal files and act on them."""
        try:
            signal_file = self.project_root / "data" / f"{self.bot_name}_signal.json"
            if not signal_file.exists():
                return
            with open(signal_file, "r", encoding="utf-8") as fh:
                cmd = json.load(fh)
            signal_file.unlink(missing_ok=True)
            command = str(cmd.get("command", "")).lower().strip()
            if command == "pause":
                self._paused = True
                logger.info("Bot '%s' paused by coordinator signal.", self.bot_name)
            elif command == "resume":
                self._paused = False
                # Clear auto_pause state from risk state file
                try:
                    if self._risk_state_file.exists():
                        with open(self._risk_state_file, "r", encoding="utf-8") as fh:
                            state = json.load(fh)
                        state.pop("auto_paused", None)
                        state.pop("auto_pause_reason", None)
                        state.pop("auto_pause_at", None)
                        state.pop("auto_resume_at", None)
                        temp_file = self._risk_state_file.with_suffix(".tmp")
                        with open(temp_file, "w", encoding="utf-8") as fh:
                            json.dump(state, fh, indent=2)
                        temp_file.replace(self._risk_state_file)
                except Exception as exc:
                    logger.warning("Could not clear auto_pause state: %s", exc)
                logger.info("Bot '%s' resumed by coordinator signal.", self.bot_name)
        except Exception as exc:
            logger.warning("Error checking coordinator signals: %s", exc)

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

