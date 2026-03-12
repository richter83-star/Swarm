"""
Safe meta-evolver for Swarm.

Important constraints:
- No self-rewrite
- No dynamic capability expansion
- No filesystem patch/synth/executor toolchain
- Variants are registered in memory only
"""

from __future__ import annotations

import copy
import hashlib
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


IMMUTABLE_AGENTS = ["GovernorAgent", "MetaEvolverAgent"]


class MetaEvolverAgent:
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        project_root: str = ".",
    ):
        self.cfg = config or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.mutation_rate = float(self.cfg.get("mutation_rate", 0.3) or 0.3)
        self.immutable_agents = set(self.cfg.get("immutable_agents", IMMUTABLE_AGENTS) or IMMUTABLE_AGENTS)

        # Hard safety constraints.
        self.allow_file_writes = False
        self.allow_self_rewrite = False

        self._variants: Dict[str, Dict[str, Any]] = {}

        db_rel = str(self.cfg.get("db_path", "data/meta_learning.db"))
        self.db_path = Path(project_root) / db_rel
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
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

    def _log_mutation(
        self,
        agent_name: str,
        old_cfg: Dict[str, Any],
        new_cfg: Dict[str, Any],
        reason: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO config_mutations
                (timestamp, bot_name, config_key, old_value, new_value, reason, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                str(agent_name),
                "meta_evolver.variant",
                str(old_cfg),
                str(new_cfg),
                str(reason),
                "meta_evolver",
            ),
        )
        self._conn.commit()

    def execute(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        context = context or {}
        if not self.enabled:
            return {"status": "disabled", "mutated_agents": []}

        agent_configs = context.get("agent_configs", {}) or {}
        mutated_agents = []

        for agent_name, base_cfg in agent_configs.items():
            new_cfg = self._mutate_agent(agent_name, dict(base_cfg or {}))
            if not new_cfg:
                continue
            variant_name = self._register_variant_in_memory(agent_name, new_cfg)
            mutated_agents.append({
                "agent": agent_name,
                "variant": variant_name,
            })

        return {
            "status": "ok",
            "mutated_agents": mutated_agents,
            "variant_count": len(self._variants),
            "trigger": context.get("trigger", "unspecified"),
        }

    def _mutate_agent(self, agent_name: str, base_config: Dict[str, Any]) -> Dict[str, Any]:
        if agent_name in IMMUTABLE_AGENTS or agent_name in self.immutable_agents:
            logger.warning(f"Skipping mutation of protected agent: {agent_name}")
            return {}

        mutated = copy.deepcopy(base_config)
        if not mutated:
            return {}

        # Conservative in-memory parameter mutation.
        if "temperature" in mutated and random.random() < self.mutation_rate:
            try:
                old_temp = float(mutated["temperature"])
                mutated["temperature"] = round(max(0.0, min(1.0, old_temp + random.uniform(-0.05, 0.05))), 3)
            except Exception:
                pass

        if "confidence_threshold" in mutated and random.random() < self.mutation_rate:
            try:
                old_threshold = float(mutated["confidence_threshold"])
                mutated["confidence_threshold"] = round(max(0.0, min(100.0, old_threshold + random.uniform(-2, 2))), 2)
            except Exception:
                pass

        self._log_mutation(agent_name, base_config, mutated, reason="in_memory_variant_mutation")
        return mutated

    def _register_variant_in_memory(self, agent_name: str, config: Dict[str, Any]) -> str:
        fingerprint = hashlib.md5(f"{agent_name}:{config}".encode("utf-8")).hexdigest()[:8]
        variant_name = f"{agent_name}_meta_{int(time.time())}_{fingerprint}"
        self._variants[variant_name] = {
            "parent": agent_name,
            "config": config,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("MetaEvolver registered in-memory variant: %s", variant_name)
        return variant_name

    def get_variants(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._variants)


