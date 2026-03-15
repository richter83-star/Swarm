"""
Cross-bot meta-learning for the Kalshi Swarm.

This module is intentionally conservative:
- No PyTorch runtime dependency
- Uses in-memory + SQLite persistence for task memory
- Supports safe strategy hints (e.g., temperature recommendations)

Cross-bot learning flow
-----------------------
1. Each bot's ``RLFeedbackBridge`` feeds settled trade outcomes into its
   own ``MetaLearner`` instance (per-bot SQLite DB).
2. ``SwarmMetaAggregator`` (run by the coordinator every N minutes) reads
   *all* bot trade DBs, computes swarm-wide category edge rates and feature
   importance rankings, and writes ``data/swarm_meta_insights.json``.
3. Each bot loads the insights file at the start of each scan cycle and
   blends the swarm-wide category multiplier with its own local multiplier
   before applying it to scored signals.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TORCH_AVAILABLE = False


META_DOMAINS: List[str] = [
    "sports",
    "politics",
    "crypto",
    "economics",
    "weather",
    "entertainment",
]


@dataclass
class TaskMeta:
    task_type: str
    task_id: str
    task_description: str
    domain: str
    final_performance: float
    learning_curve: List[float]
    hyperparameters: Dict[str, Any]
    learned_at: str


class MetaLearner:
    """
    Minimal, production-safe meta learner used by Swarm.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        project_root: str = ".",
        bot_name: str = "",
    ):
        self.cfg = config or {}
        self.bot_name = bot_name
        self.domains: List[str] = list(self.cfg.get("domains", META_DOMAINS))
        self.task_memory: Dict[str, TaskMeta] = {}

        # Force-disable torch path until VPS confirms runtime support.
        global TORCH_AVAILABLE
        TORCH_AVAILABLE = False
        self.torch_available = TORCH_AVAILABLE

        db_rel = str(self.cfg.get("db_path", "data/meta_learning.db"))
        self.db_path = Path(project_root) / db_rel
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._load_recent_tasks(limit=5000)

        logger.info(
            "MetaLearner initialized for %s | tasks=%d | torch_available=%s",
            self.bot_name or "swarm",
            len(self.task_memory),
            self.torch_available,
        )

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta_tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT,
                task_description TEXT,
                domain TEXT,
                final_performance REAL,
                learning_curve TEXT,
                hyperparameters TEXT,
                learned_at TEXT
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS config_mutations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                bot_name TEXT,
                config_key TEXT,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                source TEXT
            )
            """
        )
        self._conn.commit()

    def _load_recent_tasks(self, limit: int = 5000) -> None:
        rows = self._conn.execute(
            """
            SELECT task_id, task_type, task_description, domain,
                   final_performance, learning_curve, hyperparameters, learned_at
            FROM meta_tasks
            ORDER BY learned_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        for row in rows:
            try:
                curve = json.loads(row["learning_curve"] or "[]")
            except Exception:
                curve = []
            try:
                hparams = json.loads(row["hyperparameters"] or "{}")
            except Exception:
                hparams = {}
            meta = TaskMeta(
                task_type=str(row["task_type"] or "market_outcome"),
                task_id=str(row["task_id"]),
                task_description=str(row["task_description"] or ""),
                domain=self._normalize_domain(str(row["domain"] or "")),
                final_performance=float(row["final_performance"] or 0.0),
                learning_curve=list(curve),
                hyperparameters=dict(hparams),
                learned_at=str(row["learned_at"] or ""),
            )
            self.task_memory[meta.task_id] = meta

    def task_count(self) -> int:
        return len(self.task_memory)

    def _normalize_domain(self, domain: str) -> str:
        raw = str(domain or "").strip().lower()
        if raw in self.domains:
            return raw
        alias_map = {
            "finance": "economics",
            "business": "economics",
            "markets": "economics",
            "elections": "politics",
            "government": "politics",
            "climate": "weather",
            "science": "weather",
            "movies": "entertainment",
            "tv": "entertainment",
        }
        for k, v in alias_map.items():
            if k in raw:
                return v
        return "entertainment"

    def learn_from_task(
        self,
        task_type: str,
        task_description: str,
        domain: str,
        learning_curve: List[float],
        final_performance: float,
        hyperparameters: Dict[str, Any],
        task_id: Optional[str] = None,
    ) -> str:
        task_id = task_id or f"{task_type}_{datetime.now(timezone.utc).timestamp()}"
        normalized_domain = self._normalize_domain(domain)
        learned_at = datetime.now(timezone.utc).isoformat()
        meta = TaskMeta(
            task_type=str(task_type or "market_outcome"),
            task_id=str(task_id),
            task_description=str(task_description or ""),
            domain=normalized_domain,
            final_performance=float(final_performance),
            learning_curve=list(learning_curve or []),
            hyperparameters=dict(hyperparameters or {}),
            learned_at=learned_at,
        )
        self.task_memory[meta.task_id] = meta
        self._conn.execute(
            """
            INSERT OR REPLACE INTO meta_tasks
                (task_id, task_type, task_description, domain,
                 final_performance, learning_curve, hyperparameters, learned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meta.task_id,
                meta.task_type,
                meta.task_description,
                meta.domain,
                meta.final_performance,
                json.dumps(meta.learning_curve, ensure_ascii=True),
                json.dumps(meta.hyperparameters, ensure_ascii=True),
                meta.learned_at,
            ),
        )
        self._conn.commit()
        return meta.task_id

    def _tokens(self, text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", str(text or "").lower()))

    def _similarity(self, a_desc: str, a_domain: str, b_desc: str, b_domain: str) -> float:
        ta = self._tokens(a_desc)
        tb = self._tokens(b_desc)
        if not ta and not tb:
            jaccard = 0.0
        else:
            denom = max(1, len(ta | tb))
            jaccard = len(ta & tb) / denom
        domain_bonus = 0.35 if a_domain == b_domain else 0.0
        return max(0.0, min(1.0, domain_bonus + (0.65 * jaccard)))

    def _default_hyperparameters(self) -> Dict[str, Any]:
        return {
            "temperature": 0.1,
            "learning_rate": 0.001,
        }

    def predict_strategy(
        self,
        task_description: str,
        domain: str,
        available_strategies: Optional[List[str]] = None,
    ) -> Tuple[str, float, Dict[str, Any]]:
        # Critical rule: skip prediction until at least 10 tasks are logged.
        if self.task_count() < 10:
            return "few_shot", 0.0, self._default_hyperparameters()

        normalized_domain = self._normalize_domain(domain)
        sims: List[Tuple[TaskMeta, float]] = []
        for meta in self.task_memory.values():
            sim = self._similarity(
                task_description,
                normalized_domain,
                meta.task_description,
                meta.domain,
            )
            if sim > 0:
                sims.append((meta, sim))
        sims.sort(key=lambda x: x[1], reverse=True)
        sims = sims[:20]

        if not sims:
            return "few_shot", 0.0, self._default_hyperparameters()

        weighted_perf = 0.0
        weight_sum = 0.0
        temps: List[float] = []
        for meta, sim in sims:
            weighted_perf += float(meta.final_performance) * sim
            weight_sum += sim
            try:
                temp = float(meta.hyperparameters.get("temperature", 0.1))
                temps.append(max(0.0, min(1.0, temp)))
            except Exception:
                pass
        avg_perf = (weighted_perf / weight_sum) if weight_sum > 0 else 0.5
        base_conf = 0.45 + (0.5 * abs(avg_perf - 0.5) * 2.0)
        density_boost = min(0.05, len(sims) * 0.005)
        confidence = max(0.0, min(1.0, base_conf + density_boost))

        if temps:
            suggested_temp = sum(temps) / len(temps)
        else:
            suggested_temp = 0.1

        if avg_perf < 0.45:
            suggested_temp = max(0.05, suggested_temp - 0.05)
        elif avg_perf > 0.55:
            suggested_temp = min(0.25, suggested_temp + 0.03)

        strategy = "transfer" if len(sims) >= 5 else "few_shot"
        if available_strategies and strategy not in available_strategies:
            strategy = available_strategies[0]

        return strategy, float(confidence), {
            "temperature": round(float(suggested_temp), 3),
            "kelly_bias": round((avg_perf - 0.5) * 2.0, 3),
            "domain": normalized_domain,
        }

    def log_config_mutation(
        self,
        bot_name: str,
        config_key: str,
        old_value: Any,
        new_value: Any,
        reason: str,
        source: str = "meta_learning",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO config_mutations
                (timestamp, bot_name, config_key, old_value, new_value, reason, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                str(bot_name or ""),
                str(config_key or ""),
                json.dumps(old_value, ensure_ascii=True, default=str),
                json.dumps(new_value, ensure_ascii=True, default=str),
                str(reason or ""),
                str(source or "meta_learning"),
            ),
        )
        self._conn.commit()

    def get_statistics(self) -> Dict[str, Any]:
        return {
            "tasks_learned": self.task_count(),
            "domains": sorted(list(set(m.domain for m in self.task_memory.values()))),
            "torch_available": self.torch_available,
        }

    # ------------------------------------------------------------------
    # Swarm insights integration
    # ------------------------------------------------------------------

    def get_swarm_category_multiplier(
        self,
        category: str,
        own_multiplier: float,
        insights: "CrossBotInsights",
        own_trades: int = 0,
        own_weight: float = 0.6,
    ) -> float:
        """
        Blend the bot's own category multiplier with the swarm-wide multiplier.

        When the bot has little local data (own_trades < 20) the swarm signal
        gets a higher weight.  When own data is ample (own_trades >= 50) the
        bot trusts itself more.

        Parameters
        ----------
        category:
            Market category string.
        own_multiplier:
            Multiplier from the bot's local LearningEngine (0.7–1.3).
        insights:
            Loaded ``CrossBotInsights`` from the coordinator.
        own_trades:
            Number of settled trades the bot has for this category.
        own_weight:
            Base weight given to own data (default 0.6).  Overridden by
            data-density logic.

        Returns
        -------
        float
            Blended multiplier clamped to [0.7, 1.3].
        """
        swarm_mult = insights.get_category_multiplier(category)
        if swarm_mult is None:
            return own_multiplier

        # Shift weight toward swarm when own data is sparse.
        if own_trades < 10:
            w_own = 0.3
        elif own_trades < 20:
            w_own = 0.45
        elif own_trades < 50:
            w_own = own_weight
        else:
            w_own = 0.75

        blended = w_own * own_multiplier + (1.0 - w_own) * swarm_mult
        return round(max(0.7, min(1.3, blended)), 4)


# ---------------------------------------------------------------------------
# Cross-bot insights container
# ---------------------------------------------------------------------------

@dataclass
class CategoryEdge:
    """Swarm-wide performance data for a single market category."""
    win_rate: float          # 0–100
    avg_pnl_cents: float
    total_trades: int
    best_bot: str            # bot name with highest win rate in this category
    feature_weights: Dict[str, float]  # dimension → weight (sums to 1.0)


@dataclass
class CrossBotInsights:
    """
    Aggregated swarm-wide learning signal shared across all bots.

    Produced by ``SwarmMetaAggregator`` and written to
    ``data/swarm_meta_insights.json``.  Bots load this file at the
    start of each scan cycle and blend its multipliers with their own.
    """
    generated_at: str
    swarm_win_rate: float
    swarm_avg_pnl_cents: float
    swarm_total_trades: int
    hot_categories: List[str]
    cold_categories: List[str]
    categories: Dict[str, CategoryEdge]       # category → edge data
    bot_summaries: Dict[str, Dict[str, Any]]  # bot_name → summary stats
    min_trades_threshold: int = 15

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_category_multiplier(self, category: str) -> Optional[float]:
        """
        Return a swarm-wide category multiplier (same formula as
        ``LearningEngine.get_category_multiplier``).

        Returns ``None`` when there is insufficient swarm data.
        """
        edge = self.categories.get(str(category or "").lower())
        if edge is None or edge.total_trades < self.min_trades_threshold:
            return None
        if self.swarm_total_trades < 20:
            return None
        swarm_wr = self.swarm_win_rate / 100.0
        cat_wr = edge.win_rate / 100.0
        mult = 1.0 + (cat_wr - swarm_wr)
        return round(max(0.7, min(1.3, mult)), 4)

    def get_feature_weights(self, category: str) -> Optional[Dict[str, float]]:
        """
        Return swarm-derived feature weights for a category, or ``None``
        if there is insufficient data.
        """
        edge = self.categories.get(str(category or "").lower())
        if edge is None or edge.total_trades < self.min_trades_threshold:
            return None
        return edge.feature_weights or None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "swarm_win_rate": self.swarm_win_rate,
            "swarm_avg_pnl_cents": self.swarm_avg_pnl_cents,
            "swarm_total_trades": self.swarm_total_trades,
            "hot_categories": self.hot_categories,
            "cold_categories": self.cold_categories,
            "min_trades_threshold": self.min_trades_threshold,
            "categories": {
                cat: {
                    "win_rate": e.win_rate,
                    "avg_pnl_cents": e.avg_pnl_cents,
                    "total_trades": e.total_trades,
                    "best_bot": e.best_bot,
                    "feature_weights": e.feature_weights,
                }
                for cat, e in self.categories.items()
            },
            "bot_summaries": self.bot_summaries,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CrossBotInsights":
        categories: Dict[str, CategoryEdge] = {}
        for cat, v in (data.get("categories") or {}).items():
            if not isinstance(v, dict):
                continue
            categories[cat] = CategoryEdge(
                win_rate=float(v.get("win_rate", 0.0)),
                avg_pnl_cents=float(v.get("avg_pnl_cents", 0.0)),
                total_trades=int(v.get("total_trades", 0)),
                best_bot=str(v.get("best_bot", "")),
                feature_weights=dict(v.get("feature_weights") or {}),
            )
        return cls(
            generated_at=str(data.get("generated_at", "")),
            swarm_win_rate=float(data.get("swarm_win_rate", 0.0)),
            swarm_avg_pnl_cents=float(data.get("swarm_avg_pnl_cents", 0.0)),
            swarm_total_trades=int(data.get("swarm_total_trades", 0)),
            hot_categories=list(data.get("hot_categories") or []),
            cold_categories=list(data.get("cold_categories") or []),
            categories=categories,
            bot_summaries=dict(data.get("bot_summaries") or {}),
            min_trades_threshold=int(data.get("min_trades_threshold", 15)),
        )


# ---------------------------------------------------------------------------
# Swarm-wide aggregator
# ---------------------------------------------------------------------------

_DIMS = ["edge_score", "liquidity_score", "volume_score", "timing_score", "momentum_score"]
_DIM_LABELS = ["edge", "liquidity", "volume", "timing", "momentum"]


class SwarmMetaAggregator:
    """
    Reads all bot trade DBs and computes cross-bot performance insights.

    Designed to be called by the ``SwarmCoordinator`` on a background thread
    every ``aggregation_interval_seconds`` (default: 1800 s / 30 min).

    The output is written atomically to ``data/swarm_meta_insights.json``
    via a temp-file rename so bots always see a consistent snapshot.

    Parameters
    ----------
    project_root:
        Project root directory.
    config:
        The ``meta_learning`` section of ``swarm_config.yaml``.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        "aggregation_interval_seconds": 1800,
        "min_trades_per_category": 10,
        "insights_max_age_seconds": 7200,
        "insights_file": "data/swarm_meta_insights.json",
        "hot_category_threshold": 0.08,   # win_rate delta above swarm avg
        "cold_category_threshold": -0.08,
    }

    def __init__(
        self,
        project_root: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        self.project_root = Path(project_root).resolve()
        self._insights_path = self.project_root / str(
            self.cfg.get("insights_file", "data/swarm_meta_insights.json")
        )
        self._insights_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_aggregation: Optional[datetime] = None
        self._interval = int(self.cfg.get("aggregation_interval_seconds", 1800))
        self._min_trades = int(self.cfg.get("min_trades_per_category", 10))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_aggregate(self) -> bool:
        if self._last_aggregation is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_aggregation).total_seconds()
        return elapsed >= self._interval

    def aggregate(
        self,
        bot_db_paths: Dict[str, Path],
    ) -> Optional[CrossBotInsights]:
        """
        Read all bot trade DBs, compute insights, write the JSON file.

        Parameters
        ----------
        bot_db_paths:
            Mapping of bot_name → Path to that bot's SQLite trade DB.

        Returns
        -------
        CrossBotInsights or None on failure.
        """
        try:
            insights = self._compute_insights(bot_db_paths)
            self._write_insights(insights)
            self._last_aggregation = datetime.now(timezone.utc)
            logger.info(
                "SwarmMetaAggregator: aggregated %d bot(s), %d total trades, "
                "%d categories, hot=%s cold=%s",
                len(bot_db_paths),
                insights.swarm_total_trades,
                len(insights.categories),
                insights.hot_categories,
                insights.cold_categories,
            )
            return insights
        except Exception as exc:
            logger.warning("SwarmMetaAggregator.aggregate failed: %s", exc)
            return None

    @classmethod
    def load_insights(
        cls,
        project_root: str,
        max_age_seconds: int = 7200,
        insights_file: str = "data/swarm_meta_insights.json",
    ) -> Optional[CrossBotInsights]:
        """
        Load the insights JSON from disk.  Returns ``None`` if the file is
        missing, unreadable, or older than ``max_age_seconds``.
        """
        path = Path(project_root) / insights_file
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            logger.debug("Failed reading swarm insights: %s", exc)
            return None

        generated_at_raw = str(data.get("generated_at", "")).strip()
        if not generated_at_raw:
            return None
        try:
            ts_txt = generated_at_raw
            if ts_txt.endswith("Z"):
                ts_txt = f"{ts_txt[:-1]}+00:00"
            ts = datetime.fromisoformat(ts_txt)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > max(1, int(max_age_seconds)):
                logger.debug("Swarm insights stale (%.0fs old).", age)
                return None
        except Exception:
            return None

        try:
            return CrossBotInsights.from_dict(data)
        except Exception as exc:
            logger.debug("Failed parsing swarm insights: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Internal computation
    # ------------------------------------------------------------------

    def _compute_insights(
        self, bot_db_paths: Dict[str, Path]
    ) -> CrossBotInsights:
        # Aggregate category data across all bot DBs.
        # Structure: category → {wins, losses, pnl_cents, dim_wins_vals, dim_all_vals}
        cat_data: Dict[str, Dict[str, Any]] = {}
        # Per-bot: category → {wins, losses} for best-bot tracking
        bot_cat: Dict[str, Dict[str, Dict[str, int]]] = {}
        bot_summaries: Dict[str, Dict[str, Any]] = {}

        for bot_name, db_path in bot_db_paths.items():
            rows, feature_rows = self._read_bot_db(db_path)
            bot_wins = sum(1 for r in rows if r["outcome"] == "win")
            bot_total = len(rows)
            bot_pnl = sum(r["pnl_cents"] for r in rows)
            bot_summaries[bot_name] = {
                "win_rate": round(bot_wins / bot_total * 100, 2) if bot_total else 0.0,
                "avg_pnl_cents": round(bot_pnl / bot_total, 2) if bot_total else 0.0,
                "total_trades": bot_total,
            }

            for r in feature_rows:
                cat = str(r.get("category") or "unknown").strip().lower()
                if not cat:
                    cat = "unknown"
                outcome = str(r.get("outcome", "")).strip()
                pnl = int(r.get("pnl_cents") or 0)
                is_win = outcome == "win"

                bucket = cat_data.setdefault(cat, {
                    "wins": 0, "total": 0, "pnl_sum": 0,
                    "dim_wins": {d: [] for d in _DIM_LABELS},
                    "dim_all":  {d: [] for d in _DIM_LABELS},
                    "binary": [],
                })
                bucket["total"] += 1
                bucket["pnl_sum"] += pnl
                bucket["binary"].append(1.0 if is_win else 0.0)
                if is_win:
                    bucket["wins"] += 1
                for dim_col, dim_label in zip(_DIMS, _DIM_LABELS):
                    v = float(r.get(dim_col) or 0.0)
                    bucket["dim_all"][dim_label].append(v)
                    if is_win:
                        bucket["dim_wins"][dim_label].append(v)

                # Track per-bot category wins for best-bot calculation
                bot_cat.setdefault(bot_name, {}).setdefault(cat, {"wins": 0, "total": 0})
                bot_cat[bot_name][cat]["total"] += 1
                if is_win:
                    bot_cat[bot_name][cat]["wins"] += 1

        # Compute swarm totals
        swarm_total = sum(v["total"] for v in cat_data.values())
        swarm_wins = sum(v["wins"] for v in cat_data.values())
        swarm_pnl = sum(v["pnl_sum"] for v in cat_data.values())
        swarm_wr = swarm_wins / swarm_total * 100.0 if swarm_total else 0.0
        swarm_avg_pnl = swarm_pnl / swarm_total if swarm_total else 0.0

        hot_thresh = float(self.cfg.get("hot_category_threshold", 0.08)) * 100.0
        cold_thresh = float(self.cfg.get("cold_category_threshold", -0.08)) * 100.0

        categories: Dict[str, CategoryEdge] = {}
        hot_cats: List[str] = []
        cold_cats: List[str] = []

        for cat, bucket in cat_data.items():
            total = bucket["total"]
            if total < self._min_trades:
                continue

            wins = bucket["wins"]
            win_rate = wins / total * 100.0
            avg_pnl = bucket["pnl_sum"] / total
            binary = bucket["binary"]

            # Feature importance: point-biserial correlation per dimension
            fi: Dict[str, float] = {}
            for label in _DIM_LABELS:
                vals = bucket["dim_all"][label]
                fi[label] = _point_biserial(vals, binary)

            # Normalize fi to weights that sum to 1.0 (shift to ≥ 0 first)
            fi_min = min(fi.values())
            fi_shifted = {d: v - fi_min for d, v in fi.items()}
            fi_sum = sum(fi_shifted.values()) or 1.0
            weights = {d: round(fi_shifted[d] / fi_sum, 4) for d in _DIM_LABELS}

            # Which bot performs best in this category?
            best_bot = ""
            best_wr = -1.0
            for bot_name, bot_cats in bot_cat.items():
                bc = bot_cats.get(cat, {})
                bc_total = bc.get("total", 0)
                if bc_total < 5:
                    continue
                bc_wr = bc.get("wins", 0) / bc_total
                if bc_wr > best_wr:
                    best_wr = bc_wr
                    best_bot = bot_name

            edge = CategoryEdge(
                win_rate=round(win_rate, 2),
                avg_pnl_cents=round(avg_pnl, 2),
                total_trades=total,
                best_bot=best_bot,
                feature_weights=weights,
            )
            categories[cat] = edge

            delta = win_rate - swarm_wr
            if delta >= hot_thresh:
                hot_cats.append(cat)
            elif delta <= cold_thresh:
                cold_cats.append(cat)

        return CrossBotInsights(
            generated_at=datetime.now(timezone.utc).isoformat(),
            swarm_win_rate=round(swarm_wr, 2),
            swarm_avg_pnl_cents=round(swarm_avg_pnl, 2),
            swarm_total_trades=swarm_total,
            hot_categories=sorted(hot_cats),
            cold_categories=sorted(cold_cats),
            categories=categories,
            bot_summaries=bot_summaries,
            min_trades_threshold=self._min_trades,
        )

    def _read_bot_db(
        self, db_path: Path
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Return (summary_rows, feature_rows) from one bot's trade DB.

        summary_rows: all settled trades (outcome + pnl only)
        feature_rows: settled trades with all 5 feature scores and category
        """
        if not db_path.exists():
            return [], []
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            conn.row_factory = sqlite3.Row
            # WAL reader — does not block bot writers
            conn.execute("PRAGMA journal_mode=WAL")
            rows = conn.execute(
                """
                SELECT outcome, pnl_cents, category,
                       edge_score, liquidity_score, volume_score,
                       timing_score, momentum_score
                FROM trades
                WHERE outcome IN ('win', 'loss')
                  AND COALESCE(pnl_valid, 1) = 1
                ORDER BY id DESC
                LIMIT 2000
                """
            ).fetchall()
            conn.close()
            feature_rows = [dict(r) for r in rows]
            return feature_rows, feature_rows
        except Exception as exc:
            logger.warning("SwarmMetaAggregator: failed reading %s: %s", db_path, exc)
            return [], []

    def _write_insights(self, insights: CrossBotInsights) -> None:
        """Atomically write insights JSON via temp-file rename."""
        tmp = self._insights_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(insights.to_dict(), fh, indent=2, ensure_ascii=True)
        tmp.replace(self._insights_path)


# ---------------------------------------------------------------------------
# Module-level helper (shared with aggregator and MetaLearner)
# ---------------------------------------------------------------------------

def _point_biserial(values: List[float], binary: List[float]) -> float:
    """Point-biserial correlation between a continuous variable and 0/1 labels."""
    import math as _math
    n = len(values)
    if n < 4 or len(binary) != n:
        return 0.0
    n1 = sum(binary)
    n0 = n - n1
    if n1 == 0 or n0 == 0:
        return 0.0
    mean_1 = sum(v for v, b in zip(values, binary) if b) / n1
    mean_0 = sum(v for v, b in zip(values, binary) if not b) / n0
    mean_all = sum(values) / n
    var = sum((v - mean_all) ** 2 for v in values) / n
    std = _math.sqrt(var) if var > 0 else 1e-9
    rpb = ((mean_1 - mean_0) / std) * _math.sqrt(n1 * n0 / (n * n))
    return round(max(-1.0, min(1.0, rpb)), 4)

