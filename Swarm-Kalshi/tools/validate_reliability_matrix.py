"""
Offline reliability validation matrix for critical runtime safeguards.

Run:
    python tools/validate_reliability_matrix.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from kalshi_agent.learning_engine import LearningEngine
from swarm.balance_manager import BalanceManager
from swarm.bot_runner import BotRunner
from swarm.central_llm_controller import CentralLLMController
from swarm.swarm_coordinator import SwarmCoordinator


class _DummyProcess:
    def __init__(self, pid: int, poll_result: Any = None):
        self.pid = pid
        self._poll_result = poll_result

    def poll(self):
        return self._poll_result


class _DummyBot:
    def __init__(self, process: _DummyProcess):
        self.process = process


def _check(name: str, cond: bool, detail: str, failures: List[str]) -> None:
    if cond:
        print(f"[PASS] {name} :: {detail}")
    else:
        print(f"[FAIL] {name} :: {detail}")
        failures.append(name)


def test_stale_status_pid(failures: List[str]) -> None:
    bot = _DummyBot(_DummyProcess(pid=1234, poll_result=None))
    stale_status = {"pid": 9999, "state": "running"}
    ok = SwarmCoordinator._status_matches_process(bot, stale_status)
    _check(
        "stale_status_old_pid",
        ok is False,
        "mismatched PID is rejected",
        failures,
    )


def test_bot_exits_before_running(failures: List[str]) -> None:
    bot = _DummyBot(_DummyProcess(pid=1234, poll_result=1))
    status = {"pid": 1234, "state": "running"}
    ok = SwarmCoordinator._status_matches_process(bot, status)
    _check(
        "bot_exits_before_running",
        ok is False,
        "exited process is not treated as running",
        failures,
    )


def test_trade_guard_stale_snapshot(failures: List[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "guard.json"
        p.write_text(
            json.dumps({"timestamp": "2000-01-01T00:00:00+00:00", "valid": True}),
            encoding="utf-8",
        )
        snap, reason = BalanceManager.load_trade_guard_snapshot(str(p), max_age_seconds=10)
        _check(
            "stale_trade_guard_snapshot",
            snap is None and "stale" in reason,
            f"reason={reason}",
            failures,
        )


def test_trade_guard_malformed_snapshot(failures: List[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "guard.json"
        p.write_text("{bad json", encoding="utf-8")
        snap, reason = BalanceManager.load_trade_guard_snapshot(str(p), max_age_seconds=10)
        _check(
            "malformed_trade_guard_snapshot",
            snap is None and "unreadable" in reason,
            f"reason={reason}",
            failures,
        )


def test_global_guard_rejects(failures: List[str]) -> None:
    bm = BalanceManager({"global_daily_loss_limit_cents": 500, "global_exposure_limit_cents": 1000})
    snapshot = {
        "timestamp": "2026-03-09T12:00:00+00:00",
        "valid": True,
        "limits": {"global_daily_loss_limit_cents": 500, "global_exposure_limit_cents": 1000},
        "metrics": {"total_daily_pnl_cents": -500, "total_exposure_cents": 200},
        "bots": {"vanguard": {"available_budget_cents": 1000}},
    }
    ok, reason = bm.can_execute_trade("vanguard", "ABC", 50, snapshot)
    _check(
        "global_daily_loss_limit_enforced",
        (not ok) and ("daily loss" in reason),
        f"reason={reason}",
        failures,
    )

    snapshot["metrics"]["total_daily_pnl_cents"] = -10
    snapshot["metrics"]["total_exposure_cents"] = 980
    ok, reason = bm.can_execute_trade("vanguard", "ABC", 50, snapshot)
    _check(
        "global_exposure_limit_enforced",
        (not ok) and ("exposure" in reason),
        f"reason={reason}",
        failures,
    )


def test_pnl_invariant_quarantine(failures: List[str]) -> None:
    ok, reason, _trace = BotRunner._validate_resolved_trade_pnl(
        {"count": 16, "entry_price": 11},
        -815,
    )
    _check(
        "pnl_invariant_violation_detected",
        (not ok) and ("theoretical" in reason),
        reason,
        failures,
    )


def test_strict_llm_reject(failures: List[str]) -> None:
    controller = CentralLLMController(
        {"enabled": True, "approval_confidence_floor": 70.0},
        project_root=str(PROJECT_ROOT),
    )
    decision = controller._normalize_result(
        {
            "decision": "reject",
            "confidence": 20.0,
            "size_multiplier": 1.0,
            "rationale": "reject",
            "red_flags": [],
        },
        {"quant_confidence": 95.0, "suggested_price": 5},
    )
    _check(
        "llm_reject_stays_reject",
        decision.decision == "reject",
        f"decision={decision.decision}",
        failures,
    )


def test_sqlite_busy_timeout_write_waits(failures: List[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "busy.db"
        engine = LearningEngine({"db_path": str(db_path)})

        # Locker holds an IMMEDIATE transaction briefly.
        locker = sqlite3.connect(str(db_path), timeout=5, check_same_thread=False)
        locker.execute("PRAGMA journal_mode=WAL")
        locker.execute("BEGIN IMMEDIATE")
        locker.execute(
            """
            INSERT INTO trades
                (timestamp, ticker, event_ticker, title, series_ticker, category, bot_name,
                 side, action, count, entry_price, confidence, outcome)
            VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                "2026-03-09T00:00:00+00:00",
                "LOCK-TEST",
                "LOCK-TEST",
                "Lock Test",
                "LOCK",
                "test",
                "vanguard",
                "yes",
                "buy",
                1,
                10,
                75.0,
            ),
        )

        result: Dict[str, Any] = {"ok": False, "err": ""}

        def _writer():
            try:
                engine.log_trade(
                    ticker="LOCK-TEST-2",
                    event_ticker="LOCK-TEST-2",
                    title="Lock Test 2",
                    side="yes",
                    action="buy",
                    count=1,
                    entry_price=10,
                    confidence=80.0,
                    category="test",
                    bot_name="vanguard",
                )
                result["ok"] = True
            except Exception as exc:
                result["err"] = str(exc)

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        time.sleep(0.8)
        locker.commit()
        locker.close()
        t.join(timeout=10)
        _check(
            "sqlite_busy_timeout_handles_lock",
            result["ok"] is True and not result["err"],
            f"err={result['err'] or 'none'}",
            failures,
        )
        engine.close()


def main() -> int:
    failures: List[str] = []
    test_stale_status_pid(failures)
    test_bot_exits_before_running(failures)
    test_trade_guard_stale_snapshot(failures)
    test_trade_guard_malformed_snapshot(failures)
    test_global_guard_rejects(failures)
    test_pnl_invariant_quarantine(failures)
    test_strict_llm_reject(failures)
    test_sqlite_busy_timeout_write_waits(failures)

    if failures:
        print("")
        print(f"FAILED checks: {', '.join(failures)}")
        return 1
    print("")
    print("All reliability matrix checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
