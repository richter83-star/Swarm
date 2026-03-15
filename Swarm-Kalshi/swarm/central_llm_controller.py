"""
central_llm_controller.py
=========================

Centralized LLM trade approval layer for swarm bots.

Uses the Anthropic API (Claude) as the default provider. Ollama is
supported as an optional local fallback via ``central_llm.provider: ollama``
in the config.

Each bot asks this controller for approval before placing any order. The
controller logs all decisions to a shared SQLite DB so every bot follows one
policy and an auditable decision trail.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ApprovalDecision:
    decision: str
    confidence: float
    size_multiplier: float
    rationale: str
    red_flags: List[str]
    decision_id: Optional[int] = None


class CentralLLMController:
    """
    Shared LLM controller that gates all trade execution.

    The controller is intentionally fail-safe: if the model is unavailable or
    returns malformed output, it rejects the trade unless fail_open is enabled.
    """

    DEFAULT_CONFIG: Dict[str, Any] = {
        "enabled": True,
        "provider": "anthropic",
        "ollama_base_url": "http://127.0.0.1:11434",
        "anthropic_api_url": "https://api.anthropic.com/v1/messages",
        "anthropic_api_key": "",
        "anthropic_model": "claude-3-5-haiku-latest",
        "model": "qwen2.5:14b",
        "timeout_seconds": 20,
        "min_approved_confidence": 55.0,
        # Rejects are fail-safe by default (no weak-reject auto-override).
        # Archetype-aware approval confidence floors.
        "approval_confidence_floor": 70.0,
        "low_volume_threshold": 1000.0,
        "low_volume_confidence_floor": 85.0,
        "wide_spread_threshold_cents": 5.0,
        "wide_spread_confidence_floor": 80.0,
        "longshot_price_threshold_cents": 10.0,
        "longshot_confidence_floor": 80.0,
        # When the LLM times out/is unavailable, allow strong quant signals through.
        "allow_quant_fallback_on_error": True,
        "max_red_flags": 2,
        "default_size_multiplier": 1.0,
        "fail_open": False,
        # Adaptive feedback loop (uses realized outcomes of prior approved trades).
        "learning_enabled": True,
        "adaptive_window": 40,
        "adaptive_min_samples": 8,
        "adaptive_cold_win_rate_pct": 42.0,
        "adaptive_cold_avg_pnl_cents": -5.0,
        "adaptive_reject_quant_floor": 72.0,
        "adaptive_max_size_when_cold": 0.70,
        "adaptive_hot_win_rate_pct": 58.0,
        "adaptive_hot_avg_pnl_cents": 3.0,
        "adaptive_size_boost": 1.10,
        # Category-level adaptive guidance injected into prompts + guardrails.
        "adaptive_prompt_enabled": True,
        "adaptive_prompt_window": 120,
        "adaptive_prompt_min_samples": 6,
        "adaptive_prompt_cold_win_rate_pct": 45.0,
        "adaptive_prompt_cold_avg_pnl_cents": -3.0,
        "adaptive_prompt_hot_win_rate_pct": 58.0,
        "adaptive_prompt_hot_avg_pnl_cents": 3.0,
        "adaptive_prompt_cold_size_cap": 0.70,
        "adaptive_prompt_hot_size_boost": 1.10,
        "adaptive_prompt_reject_quant_floor": 70.0,
        "category_hint_cache_ttl_seconds": 120,
        "category_hint_max_entries": 8,
        "db_path": "data/central_llm_controller.db",
    }

    def __init__(self, config: Optional[Dict[str, Any]], project_root: str):
        self.cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        self._enabled = bool(self.cfg.get("enabled", False))
        self._provider = str(self.cfg.get("provider", "anthropic")).lower()
        self._project_root = Path(project_root).resolve()
        self._db_path = self._project_root / str(self.cfg.get("db_path", "data/central_llm_controller.db"))
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._category_hint_cache: Dict[str, Dict[str, Any]] = {}
        self._init_db()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def review_trade(
        self,
        bot_name: str,
        trade_request: Dict[str, Any],
    ) -> ApprovalDecision:
        """Approve/reject a trade request via centralized LLM policy."""
        if not self._enabled:
            return ApprovalDecision(
                decision="approve",
                confidence=float(trade_request.get("quant_confidence", 0.0)),
                size_multiplier=float(self.cfg.get("default_size_multiplier", 1.0)),
                rationale="Central LLM controller disabled.",
                red_flags=[],
            )

        try:
            llm_result = self._evaluate_with_llm(bot_name, trade_request)
            decision = self._normalize_result(llm_result, trade_request)
        except Exception as exc:
            logger.warning("Central LLM evaluation failed: %s", exc)
            quant_conf = float(trade_request.get("quant_confidence", 0.0))
            min_conf = float(self.cfg.get("min_approved_confidence", 55.0))
            if bool(self.cfg.get("allow_quant_fallback_on_error", True)) and quant_conf >= min_conf:
                decision = ApprovalDecision(
                    decision="approve",
                    confidence=quant_conf,
                    size_multiplier=float(self.cfg.get("default_size_multiplier", 1.0)),
                    rationale=f"LLM failed; quant fallback applied: {exc}",
                    red_flags=["llm_unavailable_quant_fallback"],
                )
            elif bool(self.cfg.get("fail_open", False)):
                decision = ApprovalDecision(
                    decision="approve",
                    confidence=quant_conf,
                    size_multiplier=float(self.cfg.get("default_size_multiplier", 1.0)),
                    rationale=f"LLM failed; fail_open applied: {exc}",
                    red_flags=["llm_unavailable"],
                )
            else:
                decision = ApprovalDecision(
                    decision="reject",
                    confidence=0.0,
                    size_multiplier=0.0,
                    rationale=f"LLM failed; fail-closed reject: {exc}",
                    red_flags=["llm_unavailable"],
                )

        decision = self._apply_feedback_policy(bot_name, trade_request, decision)
        decision = self._apply_category_feedback_policy(bot_name, trade_request, decision)
        decision.decision_id = self._log_decision(bot_name, trade_request, decision)
        return decision

    # ------------------------------------------------------------------
    # Core LLM integration
    # ------------------------------------------------------------------

    def _evaluate_with_llm(self, bot_name: str, trade_request: Dict[str, Any]) -> Dict[str, Any]:
        if self._provider == "ollama":
            return self._query_ollama(bot_name, trade_request)
        if self._provider in {"anthropic", "claude"}:
            return self._query_anthropic(bot_name, trade_request)
        raise RuntimeError(f"Unsupported central LLM provider: {self._provider}")

    def _query_ollama(self, bot_name: str, trade_request: Dict[str, Any]) -> Dict[str, Any]:
        base_url = str(self.cfg.get("ollama_base_url", "http://127.0.0.1:11434")).rstrip("/")
        endpoint = f"{base_url}/api/chat"
        model = str(self.cfg.get("model", "qwen2.5:14b"))
        timeout = int(self.cfg.get("timeout_seconds", 20))

        prompt = self._build_prompt(bot_name, trade_request)
        payload = json.dumps({
            "model": model,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the central risk controller for a multi-bot trading swarm. "
                        "Return ONLY valid JSON with keys: decision, confidence, size_multiplier, rationale, red_flags. "
                        "decision must be 'approve' or 'reject'. confidence is 0-100. "
                        "size_multiplier is 0.0-1.5. red_flags is an array of short strings."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0.1},
        }).encode("utf-8")

        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc

        message = body.get("message", {}) if isinstance(body, dict) else {}
        content = (message.get("content") or "").strip()
        if not content:
            raise RuntimeError("Ollama response missing content.")

        # Allow fenced JSON responses.
        if content.startswith("```"):
            parts = content.split("```")
            content = parts[1] if len(parts) > 1 else content
            if content.lstrip().startswith("json"):
                content = content.lstrip()[4:].strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Ollama returned non-JSON content: {content[:200]}") from exc

    def _query_anthropic(self, bot_name: str, trade_request: Dict[str, Any]) -> Dict[str, Any]:
        endpoint = str(self.cfg.get("anthropic_api_url", "https://api.anthropic.com/v1/messages")).strip()
        timeout = int(self.cfg.get("timeout_seconds", 20))
        model = str(
            self.cfg.get("anthropic_model")
            or self.cfg.get("model")
            or "claude-3-5-haiku-latest"
        )
        api_key = str(self.cfg.get("anthropic_api_key") or "").strip() or str(
            os.environ.get("ANTHROPIC_API_KEY", "")
        ).strip()
        if not api_key:
            raise RuntimeError("Anthropic API key missing (set central_llm.anthropic_api_key or ANTHROPIC_API_KEY).")

        prompt = self._build_prompt(bot_name, trade_request)
        system_prompt = (
            "You are the central risk controller for a multi-bot trading swarm. "
            "Return ONLY valid JSON with keys: decision, confidence, size_multiplier, rationale, red_flags. "
            "decision must be 'approve' or 'reject'. confidence is 0-100. "
            "size_multiplier is 0.0-1.5. red_flags is an array of short strings."
        )
        payload = json.dumps(
            {
                "model": model,
                "max_tokens": 350,
                "temperature": 0.1,
                "system": system_prompt,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Anthropic request failed: {exc}") from exc

        content_blocks = body.get("content", []) if isinstance(body, dict) else []
        text_parts = []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if isinstance(block, dict) and str(block.get("type", "")) == "text":
                    text_parts.append(str(block.get("text", "")))
        content = "\n".join(text_parts).strip()
        if not content:
            raise RuntimeError("Anthropic response missing text content.")

        if content.startswith("```"):
            parts = content.split("```")
            content = parts[1] if len(parts) > 1 else content
            if content.lstrip().startswith("json"):
                content = content.lstrip()[4:].strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Anthropic returned non-JSON content: {content[:200]}") from exc

    # ------------------------------------------------------------------
    # Decision normalization and logging
    # ------------------------------------------------------------------

    def _normalize_result(
        self,
        llm_result: Dict[str, Any],
        trade_request: Dict[str, Any],
    ) -> ApprovalDecision:
        raw_decision = str(llm_result.get("decision", "reject")).strip().lower()
        decision = "approve" if raw_decision == "approve" else "reject"

        confidence = float(llm_result.get("confidence", 0.0))
        confidence = max(0.0, min(100.0, confidence))

        size_multiplier = float(
            llm_result.get("size_multiplier", self.cfg.get("default_size_multiplier", 1.0))
        )
        size_multiplier = max(0.0, min(1.5, size_multiplier))

        rationale = str(llm_result.get("rationale", "")).strip() or "No rationale provided."
        red_flags = llm_result.get("red_flags", [])
        if not isinstance(red_flags, list):
            red_flags = [str(red_flags)]
        red_flags = [str(flag)[:80] for flag in red_flags][:5]

        llm_confidence = confidence
        quant_conf = float(trade_request.get("quant_confidence", 0.0))
        min_conf = float(self.cfg.get("min_approved_confidence", 55.0))
        approval_floor = self._approval_confidence_floor(trade_request)
        max_flags = int(self.cfg.get("max_red_flags", 2))

        if decision == "approve" and confidence < approval_floor:
            decision = "reject"
            rationale = (
                f"{rationale} | Auto-rejected by approval floor "
                f"(confidence={confidence:.1f} < floor={approval_floor:.1f})."
            )

        if decision == "approve" and (confidence < min_conf or len(red_flags) > max_flags):
            decision = "reject"
            rationale = (
                f"{rationale} | Auto-rejected by guardrails "
                f"(confidence={confidence:.1f}, red_flags={len(red_flags)})."
            )

        # Never approve with zero size.
        if decision == "approve" and size_multiplier <= 0.0:
            decision = "reject"
            rationale = f"{rationale} | size_multiplier was 0.0."

        # Keep quant confidence visible in logs for auditing.
        rationale = f"{rationale} | quant_confidence={quant_conf:.1f}"

        return ApprovalDecision(
            decision=decision,
            confidence=confidence,
            size_multiplier=size_multiplier,
            rationale=rationale,
            red_flags=red_flags,
        )

    def _approval_confidence_floor(self, trade_request: Dict[str, Any]) -> float:
        """
        Compute a stricter floor for low-quality market archetypes.
        """
        floor = float(self.cfg.get("approval_confidence_floor", 70.0))
        try:
            volume = float(trade_request.get("volume_24h", 0) or 0)
        except Exception:
            volume = 0.0
        try:
            spread = float(trade_request.get("spread_cents", 0) or 0)
        except Exception:
            spread = 0.0
        try:
            price = float(trade_request.get("suggested_price", 0) or 0)
        except Exception:
            price = 0.0

        if volume > 0 and volume < float(self.cfg.get("low_volume_threshold", 1000.0)):
            floor = max(floor, float(self.cfg.get("low_volume_confidence_floor", 85.0)))
        if spread > float(self.cfg.get("wide_spread_threshold_cents", 5.0)):
            floor = max(floor, float(self.cfg.get("wide_spread_confidence_floor", 80.0)))
        if 0 < price <= float(self.cfg.get("longshot_price_threshold_cents", 10.0)):
            floor = max(floor, float(self.cfg.get("longshot_confidence_floor", 80.0)))
        return max(0.0, min(100.0, floor))

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    bot_name TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quant_confidence REAL,
                    llm_confidence REAL,
                    decision TEXT NOT NULL,
                    size_multiplier REAL,
                    rationale TEXT,
                    red_flags TEXT,
                    request_json TEXT,
                    executed INTEGER NOT NULL DEFAULT 0,
                    order_id TEXT,
                    trade_db_id INTEGER,
                    outcome TEXT,
                    pnl_cents INTEGER,
                    resolved_at TEXT
                )
                """
            )
            self._migrate_db(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate_db(self, conn: sqlite3.Connection) -> None:
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(llm_decisions)").fetchall()
        }
        additions = {
            "executed": "INTEGER NOT NULL DEFAULT 0",
            "order_id": "TEXT",
            "trade_db_id": "INTEGER",
            "outcome": "TEXT",
            "pnl_cents": "INTEGER",
            "resolved_at": "TEXT",
        }
        for col, ddl in additions.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE llm_decisions ADD COLUMN {col} {ddl}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_decisions_trade_db_id ON llm_decisions(trade_db_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_decisions_outcome ON llm_decisions(outcome)"
        )
        cur = conn.execute(
            """
            UPDATE llm_decisions
            SET outcome = 'breakeven'
            WHERE outcome = 'win'
              AND COALESCE(pnl_cents, 0) = 0
            """
        )
        if (cur.rowcount or 0) > 0:
            logger.info(
                "Central LLM migration: relabeled %d decision outcome(s) win -> breakeven (zero P&L).",
                cur.rowcount,
            )

    def _log_decision(
        self,
        bot_name: str,
        trade_request: Dict[str, Any],
        decision: ApprovalDecision,
    ) -> int:
        conn = sqlite3.connect(str(self._db_path))
        try:
            cur = conn.execute(
                """
                INSERT INTO llm_decisions
                (timestamp, bot_name, ticker, side, quant_confidence, llm_confidence,
                 decision, size_multiplier, rationale, red_flags, request_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    bot_name,
                    str(trade_request.get("ticker", "")),
                    str(trade_request.get("side", "")),
                    float(trade_request.get("quant_confidence", 0.0)),
                    float(decision.confidence),
                    decision.decision,
                    float(decision.size_multiplier),
                    decision.rationale,
                    json.dumps(decision.red_flags),
                    json.dumps(trade_request),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def record_execution(
        self,
        decision_id: Optional[int],
        order_id: str,
        trade_db_id: int,
    ) -> None:
        """Attach execution metadata to a prior LLM decision row."""
        if decision_id is None:
            return
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(
                """
                UPDATE llm_decisions
                SET executed = 1, order_id = ?, trade_db_id = ?
                WHERE id = ?
                """,
                (str(order_id), int(trade_db_id), int(decision_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def record_trade_outcome(
        self,
        bot_name: str,
        ticker: str,
        outcome: str,
        pnl_cents: int,
        trade_db_id: Optional[int] = None,
        order_id: Optional[str] = None,
    ) -> None:
        """Attach realized outcome for feedback learning."""
        outcome = str(outcome or "").lower().strip()
        if outcome not in {"win", "loss", "breakeven", "expired"}:
            return

        conn = sqlite3.connect(str(self._db_path))
        try:
            row = None
            if trade_db_id is not None:
                row = conn.execute(
                    "SELECT id FROM llm_decisions WHERE trade_db_id = ? ORDER BY id DESC LIMIT 1",
                    (int(trade_db_id),),
                ).fetchone()
            if row is None and order_id:
                row = conn.execute(
                    "SELECT id FROM llm_decisions WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                    (str(order_id),),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT id FROM llm_decisions
                    WHERE bot_name = ? AND ticker = ? AND outcome IS NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (bot_name, ticker),
                ).fetchone()
            if row is None:
                return

            conn.execute(
                """
                UPDATE llm_decisions
                SET outcome = ?, pnl_cents = ?, resolved_at = ?, executed = 1
                WHERE id = ?
                """,
                (
                    outcome,
                    int(pnl_cents),
                    datetime.now(timezone.utc).isoformat(),
                    int(row[0]),
                ),
            )
            conn.commit()
            # Outcome updates should be reflected immediately in adaptive hints.
            self._category_hint_cache.pop(bot_name, None)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Prompt shaping
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_trade_category(trade_request: Dict[str, Any]) -> str:
        """Best-effort category extraction from a trade request payload."""
        for key in ("category", "market_category", "topic", "series_ticker", "event_ticker"):
            value = str(trade_request.get(key, "") or "").strip().lower()
            if value:
                return value
        return "unknown"

    def _category_feedback_hints(self, bot_name: str) -> Dict[str, Any]:
        """
        Build category-level hot/cold hints from realized outcomes.

        These hints are used in two places:
        1) Prompt shaping (so the LLM can reason with historical edges).
        2) Deterministic post-LLM guardrails for safety and consistency.
        """
        if not bool(self.cfg.get("adaptive_prompt_enabled", True)):
            return {"enabled": False}

        now = datetime.now(timezone.utc)
        ttl = max(10, int(self.cfg.get("category_hint_cache_ttl_seconds", 120)))
        cached = self._category_hint_cache.get(bot_name)
        if cached and cached.get("at"):
            age = (now - cached["at"]).total_seconds()
            if age <= ttl:
                return dict(cached.get("hints", {}))

        window = max(20, int(self.cfg.get("adaptive_prompt_window", 120)))
        min_samples = max(2, int(self.cfg.get("adaptive_prompt_min_samples", 6)))
        cold_wr = float(self.cfg.get("adaptive_prompt_cold_win_rate_pct", 45.0))
        cold_pnl = float(self.cfg.get("adaptive_prompt_cold_avg_pnl_cents", -3.0))
        hot_wr = float(self.cfg.get("adaptive_prompt_hot_win_rate_pct", 58.0))
        hot_pnl = float(self.cfg.get("adaptive_prompt_hot_avg_pnl_cents", 3.0))
        max_entries = max(1, int(self.cfg.get("category_hint_max_entries", 8)))

        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT outcome, pnl_cents, request_json
                FROM llm_decisions
                WHERE bot_name = ?
                  AND decision = 'approve'
                  AND outcome IN ('win', 'loss', 'breakeven', 'expired')
                ORDER BY id DESC
                LIMIT ?
                """,
                (bot_name, window),
            ).fetchall()
        finally:
            conn.close()

        by_category: Dict[str, Dict[str, float]] = {}
        for row in rows:
            try:
                req = json.loads(str(row["request_json"] or "{}"))
            except Exception:
                req = {}
            category = self._extract_trade_category(req)
            stats = by_category.setdefault(
                category,
                {"samples": 0.0, "wins": 0.0, "total_pnl": 0.0},
            )
            stats["samples"] += 1.0
            if str(row["outcome"] or "").lower() == "win":
                stats["wins"] += 1.0
            stats["total_pnl"] += float(row["pnl_cents"] or 0)

        cold: List[Dict[str, Any]] = []
        hot: List[Dict[str, Any]] = []
        for category, stats in by_category.items():
            samples = int(stats["samples"])
            if samples < min_samples:
                continue
            win_rate_pct = (float(stats["wins"]) / samples) * 100.0 if samples else 0.0
            avg_pnl = float(stats["total_pnl"]) / samples if samples else 0.0
            item = {
                "category": category,
                "samples": samples,
                "win_rate_pct": round(win_rate_pct, 2),
                "avg_pnl_cents": round(avg_pnl, 2),
            }
            if win_rate_pct <= cold_wr and avg_pnl <= cold_pnl:
                cold.append(item)
            elif win_rate_pct >= hot_wr and avg_pnl >= hot_pnl:
                hot.append(item)

        # Prioritize strongest signals.
        cold.sort(key=lambda i: (i["win_rate_pct"], i["avg_pnl_cents"]))
        hot.sort(key=lambda i: (-i["win_rate_pct"], -i["avg_pnl_cents"]))

        hints = {
            "enabled": True,
            "window": window,
            "min_samples": min_samples,
            "cold_categories": cold[:max_entries],
            "hot_categories": hot[:max_entries],
            "cold_size_cap": float(self.cfg.get("adaptive_prompt_cold_size_cap", 0.70)),
            "hot_size_boost": float(self.cfg.get("adaptive_prompt_hot_size_boost", 1.10)),
            "reject_quant_floor": float(self.cfg.get("adaptive_prompt_reject_quant_floor", 70.0)),
        }
        self._category_hint_cache[bot_name] = {"at": now, "hints": dict(hints)}
        return hints

    def _apply_category_feedback_policy(
        self,
        bot_name: str,
        trade_request: Dict[str, Any],
        decision: ApprovalDecision,
    ) -> ApprovalDecision:
        """Apply deterministic category-level guardrails based on learned outcomes."""
        if decision.decision != "approve":
            return decision
        hints = self._category_feedback_hints(bot_name)
        if not bool(hints.get("enabled", False)):
            return decision

        category = self._extract_trade_category(trade_request)
        if not category:
            return decision

        quant_conf = float(trade_request.get("quant_confidence", 0.0))
        reject_floor = float(hints.get("reject_quant_floor", 70.0))
        cold_size_cap = float(hints.get("cold_size_cap", 0.70))
        hot_size_boost = float(hints.get("hot_size_boost", 1.10))

        cold_map = {str(i.get("category", "")): i for i in hints.get("cold_categories", [])}
        hot_map = {str(i.get("category", "")): i for i in hints.get("hot_categories", [])}

        if category in cold_map:
            c = cold_map[category]
            if quant_conf < reject_floor:
                decision.decision = "reject"
                decision.size_multiplier = 0.0
                decision.rationale = (
                    f"{decision.rationale} | Category policy reject: {category} is cold "
                    f"(samples={c.get('samples')}, wr={c.get('win_rate_pct')}%, avg_pnl={c.get('avg_pnl_cents')}¢) "
                    f"and quant_confidence={quant_conf:.1f} < {reject_floor:.1f}."
                )
                decision.red_flags = list(decision.red_flags) + ["category_cold_reject"]
                return decision

            decision.size_multiplier = min(decision.size_multiplier, cold_size_cap)
            decision.rationale = (
                f"{decision.rationale} | Category size cap: {category} cold "
                f"(samples={c.get('samples')}, wr={c.get('win_rate_pct')}%, avg_pnl={c.get('avg_pnl_cents')}¢)."
            )
            decision.red_flags = list(decision.red_flags) + ["category_cold_size_cap"]
            return decision

        if category in hot_map:
            h = hot_map[category]
            boosted = decision.size_multiplier * hot_size_boost
            decision.size_multiplier = min(1.5, max(decision.size_multiplier, boosted))
            decision.rationale = (
                f"{decision.rationale} | Category boost: {category} hot "
                f"(samples={h.get('samples')}, wr={h.get('win_rate_pct')}%, avg_pnl={h.get('avg_pnl_cents')}¢)."
            )
            return decision

        return decision

    def _build_prompt(self, bot_name: str, trade_request: Dict[str, Any]) -> str:
        snapshot = self._swarm_snapshot()
        feedback = self._feedback_profile(bot_name)
        category_hints = self._category_feedback_hints(bot_name)
        return (
            f"Bot name: {bot_name}\n"
            f"Trade request: {json.dumps(trade_request, ensure_ascii=True)}\n"
            f"Swarm snapshot: {json.dumps(snapshot, ensure_ascii=True)}\n\n"
            f"Recent realized performance profile: {json.dumps(feedback, ensure_ascii=True)}\n\n"
            f"Category feedback hints: {json.dumps(category_hints, ensure_ascii=True)}\n\n"
            "Policy:\n"
            "1) Protect capital first.\n"
            "2) Reject trades with weak confidence or obvious concentration risk.\n"
            "3) Use size_multiplier < 1.0 when risk is elevated.\n"
            "4) Follow cold/hot category hints unless current request has very strong evidence.\n"
            "5) Keep rationale concise and concrete.\n"
        )

    def _feedback_profile(self, bot_name: str) -> Dict[str, Any]:
        """Summarize recent realized outcomes for adaptive behavior."""
        if not bool(self.cfg.get("learning_enabled", True)):
            return {"enabled": False}

        window = max(1, int(self.cfg.get("adaptive_window", 40)))
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT outcome, pnl_cents
                FROM llm_decisions
                WHERE bot_name = ?
                  AND decision = 'approve'
                  AND outcome IN ('win', 'loss', 'breakeven', 'expired')
                ORDER BY id DESC
                LIMIT ?
                """,
                (bot_name, window),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {
                "enabled": True,
                "samples": 0,
                "win_rate_pct": 0.0,
                "avg_pnl_cents": 0.0,
                "total_pnl_cents": 0,
            }

        samples = len(rows)
        wins = sum(1 for r in rows if r["outcome"] == "win")
        breakeven = sum(1 for r in rows if r["outcome"] == "breakeven")
        total_pnl = sum(int(r["pnl_cents"] or 0) for r in rows)
        return {
            "enabled": True,
            "samples": samples,
            "win_rate_pct": round((wins / samples) * 100.0, 2),
            "breakeven_pct": round((breakeven / samples) * 100.0, 2),
            "avg_pnl_cents": round(total_pnl / samples, 2),
            "total_pnl_cents": int(total_pnl),
        }

    def _apply_feedback_policy(
        self,
        bot_name: str,
        trade_request: Dict[str, Any],
        decision: ApprovalDecision,
    ) -> ApprovalDecision:
        """Use realized outcomes to adapt approval strictness and sizing."""
        if not bool(self.cfg.get("learning_enabled", True)):
            return decision

        profile = self._feedback_profile(bot_name)
        samples = int(profile.get("samples", 0))
        min_samples = int(self.cfg.get("adaptive_min_samples", 8))
        if samples < min_samples:
            return decision

        win_rate = float(profile.get("win_rate_pct", 0.0))
        avg_pnl = float(profile.get("avg_pnl_cents", 0.0))
        quant_conf = float(trade_request.get("quant_confidence", 0.0))

        cold_wr = float(self.cfg.get("adaptive_cold_win_rate_pct", 42.0))
        cold_pnl = float(self.cfg.get("adaptive_cold_avg_pnl_cents", -5.0))
        hot_wr = float(self.cfg.get("adaptive_hot_win_rate_pct", 58.0))
        hot_pnl = float(self.cfg.get("adaptive_hot_avg_pnl_cents", 3.0))
        reject_floor = float(self.cfg.get("adaptive_reject_quant_floor", 72.0))
        cold_size_cap = float(self.cfg.get("adaptive_max_size_when_cold", 0.70))
        hot_boost = float(self.cfg.get("adaptive_size_boost", 1.10))

        if win_rate <= cold_wr and avg_pnl <= cold_pnl:
            if decision.decision == "approve" and quant_conf < reject_floor:
                decision.decision = "reject"
                decision.size_multiplier = 0.0
                decision.rationale = (
                    f"{decision.rationale} | Feedback guardrail: recent approved trades are underperforming "
                    f"(samples={samples}, win_rate={win_rate:.1f}%, avg_pnl={avg_pnl:.1f}¢), "
                    f"and quant_confidence={quant_conf:.1f} < reject_floor={reject_floor:.1f}."
                )
                decision.red_flags = list(decision.red_flags) + ["feedback_cold_reject"]
                return decision
            if decision.decision == "approve":
                decision.size_multiplier = min(decision.size_multiplier, cold_size_cap)
                decision.rationale = (
                    f"{decision.rationale} | Feedback sizing cap applied due to cold streak "
                    f"(samples={samples}, win_rate={win_rate:.1f}%, avg_pnl={avg_pnl:.1f}¢)."
                )
                decision.red_flags = list(decision.red_flags) + ["feedback_cold_size_cap"]
                return decision

        if decision.decision == "approve" and win_rate >= hot_wr and avg_pnl >= hot_pnl:
            boosted = decision.size_multiplier * hot_boost
            decision.size_multiplier = min(1.5, max(decision.size_multiplier, boosted))
            decision.rationale = (
                f"{decision.rationale} | Feedback size boost applied "
                f"(samples={samples}, win_rate={win_rate:.1f}%, avg_pnl={avg_pnl:.1f}¢)."
            )
            return decision

        return decision

    def _swarm_snapshot(self) -> Dict[str, Any]:
        """Read all bot status files for central context."""
        data_dir = self._project_root / "data"
        snapshot: Dict[str, Any] = {"bots": {}}
        bot_names = ("sentinel", "oracle", "pulse", "vanguard")
        for bot_name in bot_names:
            status_path = data_dir / f"{bot_name}_status.json"
            if not status_path.exists():
                snapshot["bots"][bot_name] = {"state": "unknown"}
                continue
            try:
                with open(status_path, "r", encoding="utf-8") as fh:
                    status = json.load(fh)
                snapshot["bots"][bot_name] = {
                    "state": status.get("state", "unknown"),
                    "balance_cents": status.get("risk", {}).get("balance_cents", 0),
                    "open_positions": status.get("risk", {}).get("open_positions", 0),
                    "daily_pnl_cents": status.get("risk", {}).get("daily_pnl_cents", 0),
                }
            except Exception as exc:
                snapshot["bots"][bot_name] = {"state": "error", "error": str(exc)}
        return snapshot

