"""
Offline sanity checks for critical Swarm safety guardrails.

Run:
    python tools/validate_safety_guards.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from swarm.balance_manager import BalanceManager
from swarm.bot_runner import BotRunner
from swarm.central_llm_controller import CentralLLMController


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_trade_guard_authorization() -> None:
    bm = BalanceManager(
        {
            "global_daily_loss_limit_cents": 500,
            "global_exposure_limit_cents": 1000,
            "budget_allocation": {"sentinel": 0.25, "oracle": 0.25, "pulse": 0.25, "vanguard": 0.25},
        }
    )
    snapshot = {
        "timestamp": "2026-03-09T12:00:00+00:00",
        "valid": True,
        "limits": {
            "global_daily_loss_limit_cents": 500,
            "global_exposure_limit_cents": 1000,
        },
        "metrics": {
            "total_daily_pnl_cents": -100,
            "total_exposure_cents": 400,
        },
        "bots": {"vanguard": {"available_budget_cents": 300}},
    }
    ok, reason = bm.can_execute_trade("vanguard", "TEST", 200, snapshot)
    _assert(ok, f"expected approve, got reject: {reason}")

    ok, reason = bm.can_execute_trade("vanguard", "TEST", 700, snapshot)
    _assert(not ok and "exposure" in reason, f"expected exposure reject, got: {ok}, {reason}")

    ok, reason = bm.can_execute_trade("vanguard", "TEST", 301, snapshot)
    _assert(not ok and "budget" in reason, f"expected budget reject, got: {ok}, {reason}")


def test_guard_snapshot_loader() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "guard.json"
        payload = {"timestamp": "2026-03-09T12:00:00+00:00", "valid": True, "limits": {}, "metrics": {}, "bots": {}}
        p.write_text(json.dumps(payload), encoding="utf-8")
        snap, reason = BalanceManager.load_trade_guard_snapshot(str(p), max_age_seconds=999999)
        _assert(snap is not None and reason == "ok", f"snapshot load failed: {reason}")


def test_pnl_invariant() -> None:
    ok, reason, _trace = BotRunner._validate_resolved_trade_pnl(
        {"count": 16, "entry_price": 11},
        -815,
    )
    _assert(not ok and "theoretical" in reason, f"expected invariant fail, got: {ok}, {reason}")

    ok, reason, _trace = BotRunner._validate_resolved_trade_pnl(
        {"count": 16, "entry_price": 11},
        -180,
    )
    _assert(ok, f"expected valid pnl, got: {ok}, {reason}")


def test_strict_llm_rejects() -> None:
    controller = CentralLLMController(
        {
            "enabled": True,
            "strict_rejects": True,
            "min_approved_confidence": 55.0,
            "approval_confidence_floor": 70.0,
            "default_size_multiplier": 1.0,
        },
        project_root=str(PROJECT_ROOT),
    )
    decision = controller._normalize_result(
        {
            "decision": "reject",
            "confidence": 30.0,
            "size_multiplier": 1.0,
            "rationale": "weak reject",
            "red_flags": [],
        },
        {"quant_confidence": 95.0, "suggested_price": 5},
    )
    _assert(decision.decision == "reject", f"expected reject to stay reject, got {decision.decision}")


def main() -> None:
    test_trade_guard_authorization()
    test_guard_snapshot_loader()
    test_pnl_invariant()
    test_strict_llm_rejects()
    print("All safety guard checks passed.")


if __name__ == "__main__":
    main()
