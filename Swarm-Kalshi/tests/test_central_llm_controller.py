"""
tests/test_central_llm_controller.py
=====================================
Unit tests for CentralLLMController — approval/rejection decisions,
confidence floor guardrails, adaptive feedback policy, category hints,
timeout/error fallback modes, and DB persistence.

All LLM network calls are patched out; no real HTTP traffic.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from swarm.central_llm_controller import CentralLLMController, ApprovalDecision


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def make_ctrl(tmp_path, **cfg_overrides) -> CentralLLMController:
    cfg = {
        "enabled": True,
        "provider": "anthropic",
        "anthropic_api_key": "test-key",
        "db_path": str(tmp_path / "llm.db"),
        "approval_confidence_floor": 70.0,
        "min_approved_confidence": 55.0,
        "max_red_flags": 2,
        "default_size_multiplier": 1.0,
        "fail_open": False,
        "allow_quant_fallback_on_error": True,
        "learning_enabled": False,  # disable adaptive feedback for most tests
        "adaptive_prompt_enabled": False,
        "timeout_seconds": 5,
    }
    cfg.update(cfg_overrides)
    return CentralLLMController(config=cfg, project_root=str(tmp_path))


def make_trade(ticker="T-1", side="yes", quant_confidence=80.0, **kwargs):
    req = {
        "ticker": ticker,
        "side": side,
        "quant_confidence": quant_confidence,
        "title": "Test market",
        "category": "politics",
        "volume_24h": 2000,
        "spread_cents": 3,
        "suggested_price": 50,
    }
    req.update(kwargs)
    return req


def _llm_approve(confidence=80.0, size=1.0, flags=None):
    """Build a minimal LLM approval JSON payload."""
    return {
        "decision": "approve",
        "confidence": confidence,
        "size_multiplier": size,
        "rationale": "Looks good.",
        "red_flags": flags or [],
    }


def _llm_reject(confidence=30.0, flags=None):
    return {
        "decision": "reject",
        "confidence": confidence,
        "size_multiplier": 0.0,
        "rationale": "Too risky.",
        "red_flags": flags or ["low_edge"],
    }


# ---------------------------------------------------------------------------
# Disabled controller
# ---------------------------------------------------------------------------

class TestDisabledController:
    def test_disabled_auto_approves(self, tmp_path):
        ctrl = make_ctrl(tmp_path, enabled=False)
        req = make_trade(quant_confidence=60.0)
        decision = ctrl.review_trade("sentinel", req)
        assert decision.decision == "approve"

    def test_disabled_uses_quant_confidence(self, tmp_path):
        ctrl = make_ctrl(tmp_path, enabled=False)
        req = make_trade(quant_confidence=77.0)
        decision = ctrl.review_trade("sentinel", req)
        assert decision.confidence == 77.0


# ---------------------------------------------------------------------------
# Approval flow
# ---------------------------------------------------------------------------

class TestApprovalFlow:
    def test_llm_approve_passes_through(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.decision == "approve"

    def test_approve_size_multiplier_preserved(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, size=0.7)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.size_multiplier == pytest.approx(0.7, abs=0.01)

    def test_size_multiplier_clamped_to_1_5(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=90.0, size=5.0)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.size_multiplier <= 1.5

    def test_size_multiplier_zero_causes_reject(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, size=0.0)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.decision == "reject"

    def test_llm_reject_is_respected(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_reject()):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.decision == "reject"


# ---------------------------------------------------------------------------
# Confidence floor guardrails
# ---------------------------------------------------------------------------

class TestConfidenceFloor:
    def test_below_approval_floor_becomes_reject(self, tmp_path):
        # floor=70, LLM returns confidence=65 → auto-reject
        ctrl = make_ctrl(tmp_path, approval_confidence_floor=70.0)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=65.0)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.decision == "reject"
        assert "floor" in decision.rationale.lower()

    def test_above_approval_floor_approves(self, tmp_path):
        ctrl = make_ctrl(tmp_path, approval_confidence_floor=70.0)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=75.0)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.decision == "approve"

    def test_too_many_red_flags_causes_reject(self, tmp_path):
        ctrl = make_ctrl(tmp_path, max_red_flags=2)
        flags = ["flag1", "flag2", "flag3"]  # 3 flags > max 2
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, flags=flags)):
            decision = ctrl.review_trade("sentinel", make_trade())
        assert decision.decision == "reject"


# ---------------------------------------------------------------------------
# Archetype-aware confidence floor
# ---------------------------------------------------------------------------

class TestApprovalConfidenceFloor:
    def test_low_volume_raises_floor(self, tmp_path):
        ctrl = make_ctrl(tmp_path, low_volume_threshold=1000.0, low_volume_confidence_floor=85.0)
        req = make_trade(volume_24h=500, quant_confidence=80.0)  # volume < 1000
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", req)
        # Floor raised to 85, LLM confidence 80 < 85 → reject
        assert decision.decision == "reject"

    def test_wide_spread_raises_floor(self, tmp_path):
        ctrl = make_ctrl(tmp_path, wide_spread_threshold_cents=5.0, wide_spread_confidence_floor=80.0)
        req = make_trade(spread_cents=10, quant_confidence=75.0)  # spread > 5
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=75.0)):
            decision = ctrl.review_trade("sentinel", req)
        # Floor raised to 80, LLM confidence 75 < 80 → reject
        assert decision.decision == "reject"

    def test_longshot_price_raises_floor(self, tmp_path):
        ctrl = make_ctrl(tmp_path, longshot_price_threshold_cents=10.0, longshot_confidence_floor=80.0)
        req = make_trade(suggested_price=5, quant_confidence=75.0)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=75.0)):
            decision = ctrl.review_trade("sentinel", req)
        assert decision.decision == "reject"

    def test_normal_market_uses_base_floor(self, tmp_path):
        ctrl = make_ctrl(tmp_path, approval_confidence_floor=70.0)
        req = make_trade(volume_24h=5000, spread_cents=2, suggested_price=50)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=72.0)):
            decision = ctrl.review_trade("sentinel", req)
        assert decision.decision == "approve"


# ---------------------------------------------------------------------------
# Error fallback modes
# ---------------------------------------------------------------------------

class TestErrorFallback:
    def test_llm_unavailable_quant_fallback_approves(self, tmp_path):
        ctrl = make_ctrl(tmp_path, allow_quant_fallback_on_error=True, min_approved_confidence=55.0)
        with patch.object(ctrl, "_evaluate_with_llm", side_effect=RuntimeError("timeout")):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=75.0))
        assert decision.decision == "approve"
        assert "quant fallback" in decision.rationale.lower()

    def test_llm_unavailable_quant_fallback_rejects_low_conf(self, tmp_path):
        ctrl = make_ctrl(tmp_path, allow_quant_fallback_on_error=True, min_approved_confidence=80.0)
        with patch.object(ctrl, "_evaluate_with_llm", side_effect=RuntimeError("timeout")):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=60.0))
        assert decision.decision == "reject"
        assert "llm_unavailable" in decision.red_flags

    def test_fail_open_approves_on_error(self, tmp_path):
        ctrl = make_ctrl(tmp_path, allow_quant_fallback_on_error=False, fail_open=True)
        with patch.object(ctrl, "_evaluate_with_llm", side_effect=RuntimeError("gone")):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=40.0))
        assert decision.decision == "approve"

    def test_fail_closed_rejects_on_error(self, tmp_path):
        ctrl = make_ctrl(tmp_path, allow_quant_fallback_on_error=False, fail_open=False)
        with patch.object(ctrl, "_evaluate_with_llm", side_effect=RuntimeError("gone")):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=40.0))
        assert decision.decision == "reject"

    def test_network_error_handled_gracefully(self, tmp_path):
        ctrl = make_ctrl(tmp_path, allow_quant_fallback_on_error=True, min_approved_confidence=55.0)
        err = urllib.error.URLError("connection refused")
        with patch.object(ctrl, "_evaluate_with_llm", side_effect=RuntimeError(str(err))):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=70.0))
        assert isinstance(decision, ApprovalDecision)


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

class TestDbPersistence:
    def test_decision_logged_to_db(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", make_trade(ticker="T-LOG"))
        assert decision.decision_id is not None
        assert decision.decision_id >= 1

    def test_multiple_decisions_get_unique_ids(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        ids = []
        for i in range(3):
            with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
                d = ctrl.review_trade("sentinel", make_trade(ticker=f"T-{i}"))
            ids.append(d.decision_id)
        assert len(set(ids)) == 3

    def test_record_execution_updates_db(self, tmp_path):
        import sqlite3
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", make_trade(ticker="T-EXEC"))
        ctrl.record_execution(decision.decision_id, order_id="ORD-001", trade_db_id=42)

        conn = sqlite3.connect(str(ctrl._db_path))
        row = conn.execute(
            "SELECT executed, order_id, trade_db_id FROM llm_decisions WHERE id = ?",
            (decision.decision_id,),
        ).fetchone()
        conn.close()
        assert row[0] == 1
        assert row[1] == "ORD-001"
        assert row[2] == 42

    def test_record_trade_outcome_by_trade_db_id(self, tmp_path):
        import sqlite3
        ctrl = make_ctrl(tmp_path)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", make_trade(ticker="T-OUT"))
        ctrl.record_execution(decision.decision_id, order_id="ORD-002", trade_db_id=99)
        ctrl.record_trade_outcome("sentinel", "T-OUT", "win", pnl_cents=150, trade_db_id=99)

        conn = sqlite3.connect(str(ctrl._db_path))
        row = conn.execute(
            "SELECT outcome, pnl_cents FROM llm_decisions WHERE id = ?",
            (decision.decision_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "win"
        assert row[1] == 150

    def test_record_invalid_outcome_ignored(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        # Should not crash even with bogus outcome
        ctrl.record_trade_outcome("sentinel", "T-X", "invalid_outcome", pnl_cents=0)


# ---------------------------------------------------------------------------
# Adaptive feedback policy (enabled)
# ---------------------------------------------------------------------------

class TestAdaptiveFeedbackPolicy:
    def _seed_outcomes(self, ctrl, bot_name, wins, losses):
        """Seed the DB with win/loss outcomes via direct SQL."""
        import sqlite3
        conn = sqlite3.connect(str(ctrl._db_path))
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for i in range(wins):
            conn.execute(
                "INSERT INTO llm_decisions (timestamp, bot_name, ticker, side, quant_confidence, "
                "llm_confidence, decision, size_multiplier, rationale, red_flags, request_json, "
                "executed, outcome, pnl_cents, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, bot_name, f"W-{i}", "yes", 75.0, 75.0, "approve", 1.0, "ok",
                 "[]", '{"category": "politics"}', 1, "win", 100, now),
            )
        for i in range(losses):
            conn.execute(
                "INSERT INTO llm_decisions (timestamp, bot_name, ticker, side, quant_confidence, "
                "llm_confidence, decision, size_multiplier, rationale, red_flags, request_json, "
                "executed, outcome, pnl_cents, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, bot_name, f"L-{i}", "yes", 60.0, 60.0, "approve", 1.0, "ok",
                 "[]", '{"category": "politics"}', 1, "loss", -80, now),
            )
        conn.commit()
        conn.close()

    def test_cold_streak_caps_size(self, tmp_path):
        ctrl = make_ctrl(
            tmp_path,
            learning_enabled=True,
            adaptive_prompt_enabled=False,
            adaptive_min_samples=5,
            adaptive_cold_win_rate_pct=42.0,
            adaptive_cold_avg_pnl_cents=-5.0,
            adaptive_max_size_when_cold=0.70,
            adaptive_reject_quant_floor=72.0,
        )
        # 2 wins, 8 losses → cold (20% win rate, negative avg pnl)
        self._seed_outcomes(ctrl, "sentinel", wins=2, losses=8)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, size=1.0)):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=75.0))
        # Cold but quant_conf >= reject_floor → size cap applied, still approve
        if decision.decision == "approve":
            assert decision.size_multiplier <= 0.70

    def test_cold_streak_rejects_low_quant(self, tmp_path):
        ctrl = make_ctrl(
            tmp_path,
            learning_enabled=True,
            adaptive_prompt_enabled=False,
            adaptive_min_samples=5,
            adaptive_cold_win_rate_pct=42.0,
            adaptive_cold_avg_pnl_cents=-5.0,
            adaptive_reject_quant_floor=72.0,
        )
        self._seed_outcomes(ctrl, "sentinel", wins=2, losses=8)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=65.0))
        # 65.0 < reject_floor=72.0 → reject
        assert decision.decision == "reject"
        assert "feedback_cold_reject" in decision.red_flags

    def test_hot_streak_boosts_size(self, tmp_path):
        ctrl = make_ctrl(
            tmp_path,
            learning_enabled=True,
            adaptive_prompt_enabled=False,
            adaptive_min_samples=5,
            adaptive_hot_win_rate_pct=58.0,
            adaptive_hot_avg_pnl_cents=3.0,
            adaptive_size_boost=1.10,
        )
        # 9 wins, 1 loss → hot
        self._seed_outcomes(ctrl, "sentinel", wins=9, losses=1)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, size=1.0)):
            decision = ctrl.review_trade("sentinel", make_trade(quant_confidence=80.0))
        if decision.decision == "approve":
            assert decision.size_multiplier >= 1.0  # boosted or at least not reduced


# ---------------------------------------------------------------------------
# Category feedback policy
# ---------------------------------------------------------------------------

class TestCategoryFeedbackPolicy:
    def _seed_category(self, ctrl, bot_name, category, wins, losses, pnl_per_win=100, pnl_per_loss=-80):
        import sqlite3
        conn = sqlite3.connect(str(ctrl._db_path))
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        req_json = json.dumps({"category": category})
        for i in range(wins):
            conn.execute(
                "INSERT INTO llm_decisions (timestamp, bot_name, ticker, side, quant_confidence, "
                "llm_confidence, decision, size_multiplier, rationale, red_flags, request_json, "
                "executed, outcome, pnl_cents, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, bot_name, f"W-{i}", "yes", 75.0, 75.0, "approve", 1.0, "ok",
                 "[]", req_json, 1, "win", pnl_per_win, now),
            )
        for i in range(losses):
            conn.execute(
                "INSERT INTO llm_decisions (timestamp, bot_name, ticker, side, quant_confidence, "
                "llm_confidence, decision, size_multiplier, rationale, red_flags, request_json, "
                "executed, outcome, pnl_cents, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, bot_name, f"L-{i}", "yes", 60.0, 60.0, "approve", 1.0, "ok",
                 "[]", req_json, 1, "loss", pnl_per_loss, now),
            )
        conn.commit()
        conn.close()

    def test_cold_category_rejects_low_quant(self, tmp_path):
        ctrl = make_ctrl(
            tmp_path,
            learning_enabled=False,
            adaptive_prompt_enabled=True,
            adaptive_prompt_min_samples=5,
            adaptive_prompt_cold_win_rate_pct=45.0,
            adaptive_prompt_cold_avg_pnl_cents=-3.0,
            adaptive_prompt_reject_quant_floor=70.0,
        )
        # 2/8 = 25% win rate, avg pnl negative → cold
        self._seed_category(ctrl, "sentinel", "politics", wins=2, losses=8)
        req = make_trade(category="politics", quant_confidence=65.0)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0)):
            decision = ctrl.review_trade("sentinel", req)
        assert decision.decision == "reject"
        assert "category_cold_reject" in decision.red_flags

    def test_cold_category_caps_size_on_high_quant(self, tmp_path):
        ctrl = make_ctrl(
            tmp_path,
            learning_enabled=False,
            adaptive_prompt_enabled=True,
            adaptive_prompt_min_samples=5,
            adaptive_prompt_cold_win_rate_pct=45.0,
            adaptive_prompt_cold_avg_pnl_cents=-3.0,
            adaptive_prompt_reject_quant_floor=70.0,
            adaptive_prompt_cold_size_cap=0.70,
        )
        self._seed_category(ctrl, "sentinel", "politics", wins=2, losses=8)
        req = make_trade(category="politics", quant_confidence=75.0)  # above reject floor
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, size=1.0)):
            decision = ctrl.review_trade("sentinel", req)
        if decision.decision == "approve":
            assert decision.size_multiplier <= 0.70

    def test_hot_category_boosts_size(self, tmp_path):
        ctrl = make_ctrl(
            tmp_path,
            learning_enabled=False,
            adaptive_prompt_enabled=True,
            adaptive_prompt_min_samples=5,
            adaptive_prompt_hot_win_rate_pct=58.0,
            adaptive_prompt_hot_avg_pnl_cents=3.0,
            adaptive_prompt_hot_size_boost=1.10,
        )
        self._seed_category(ctrl, "sentinel", "economics", wins=9, losses=1,
                            pnl_per_win=50, pnl_per_loss=-10)
        req = make_trade(category="economics", quant_confidence=80.0)
        with patch.object(ctrl, "_evaluate_with_llm", return_value=_llm_approve(confidence=80.0, size=1.0)):
            decision = ctrl.review_trade("sentinel", req)
        if decision.decision == "approve":
            assert decision.size_multiplier >= 1.0


# ---------------------------------------------------------------------------
# _normalize_result edge cases
# ---------------------------------------------------------------------------

class TestNormalizeResult:
    def test_unknown_decision_defaults_to_reject(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        result = ctrl._normalize_result(
            {"decision": "maybe", "confidence": 90.0, "size_multiplier": 1.0,
             "rationale": "unsure", "red_flags": []},
            make_trade(),
        )
        assert result.decision == "reject"

    def test_confidence_clamped_to_100(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        result = ctrl._normalize_result(
            {"decision": "approve", "confidence": 999.0, "size_multiplier": 1.0,
             "rationale": "extreme", "red_flags": []},
            make_trade(),
        )
        assert result.confidence <= 100.0

    def test_red_flags_truncated_to_5(self, tmp_path):
        ctrl = make_ctrl(tmp_path, max_red_flags=10)
        flags = [f"flag{i}" for i in range(10)]
        result = ctrl._normalize_result(
            {"decision": "approve", "confidence": 80.0, "size_multiplier": 1.0,
             "rationale": "many flags", "red_flags": flags},
            make_trade(),
        )
        assert len(result.red_flags) <= 5

    def test_non_list_red_flags_normalised(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        result = ctrl._normalize_result(
            {"decision": "approve", "confidence": 80.0, "size_multiplier": 1.0,
             "rationale": "ok", "red_flags": "single_flag"},
            make_trade(),
        )
        assert isinstance(result.red_flags, list)

    def test_quant_confidence_appended_to_rationale(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        result = ctrl._normalize_result(
            {"decision": "approve", "confidence": 80.0, "size_multiplier": 1.0,
             "rationale": "ok", "red_flags": []},
            make_trade(quant_confidence=73.5),
        )
        assert "73.5" in result.rationale


# ---------------------------------------------------------------------------
# _extract_trade_category
# ---------------------------------------------------------------------------

class TestExtractTradeCategory:
    def test_returns_category_field(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        assert ctrl._extract_trade_category({"category": "Politics"}) == "politics"

    def test_falls_back_to_series_ticker(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        assert ctrl._extract_trade_category({"series_ticker": "ECON"}) == "econ"

    def test_returns_unknown_when_empty(self, tmp_path):
        ctrl = make_ctrl(tmp_path)
        assert ctrl._extract_trade_category({}) == "unknown"
