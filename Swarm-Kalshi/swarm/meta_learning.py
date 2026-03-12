"""
Lightweight meta-learning bridge for Swarm runtime adaptation.

This module is intentionally conservative:
- No PyTorch runtime dependency (forced off until VPS validation)
- Uses in-memory + SQLite persistence for task memory
- Supports safe strategy hints (e.g., temperature recommendations)
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

