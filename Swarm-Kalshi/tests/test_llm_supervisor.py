"""
test_llm_supervisor.py
======================

Unit tests for the Autonomous LLM System Supervisor & Meta-Auditor.
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from swarm.llm_supervisor import LLMSupervisor, SupervisorFinding


@pytest.fixture
def temp_project(tmp_path):
    """Create a mock project directory with data and logs subdirectories."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    return tmp_path


def test_supervisor_initialization(temp_project):
    """Verify supervisor initializes SQLite database and defaults cleanly."""
    sup = LLMSupervisor(project_root=temp_project)
    assert sup._db_path.exists()
    assert sup.cfg["enabled"] is True

    # Verify table schema
    with sqlite3.connect(str(sup._db_path)) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='supervisor_audits'")
        assert cur.fetchone() is not None


def test_audit_stale_trades_auto_reconciliation(temp_project):
    """Verify stale pending trades (>48h) are detected and auto-reconciled."""
    data_dir = temp_project / "data"
    vanguard_db = data_dir / "vanguard.db"

    # Create dummy trade 72 hours old
    old_time = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with sqlite3.connect(str(vanguard_db)) as conn:
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                ticker TEXT,
                side TEXT,
                entry_price INTEGER,
                outcome TEXT,
                pnl_cents INTEGER,
                category TEXT,
                confidence REAL,
                settled_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO trades (id, timestamp, ticker, side, entry_price, outcome, pnl_cents, category, confidence) "
            "VALUES (1, ?, 'KXOLD-TEST-1', 'yes', 50, 'pending', NULL, 'politics', 75.0)",
            (old_time,)
        )
        conn.commit()

    sup = LLMSupervisor(
        config={"auto_remediation_enabled": True, "stale_trade_max_age_hours": 48.0},
        project_root=temp_project,
    )
    report = sup.run_audit()

    assert report["total_findings"] >= 1
    assert any(f["ticker"] == "KXOLD-TEST-1" for f in report["findings"])
    assert any(r["type"] == "stale_trade_cleanup" for r in report["auto_remediations"])

    # Verify trade in database was updated to 'expired'
    with sqlite3.connect(str(vanguard_db)) as conn:
        row = conn.execute("SELECT outcome, pnl_cents FROM trades WHERE id = 1").fetchone()
        assert row[0] == "expired"
        assert row[1] == 0


def test_audit_category_throttling(temp_project):
    """Verify categories with low win rates (<40%) trigger auto-throttling."""
    data_dir = temp_project / "data"
    sentinel_db = data_dir / "sentinel.db"

    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(sentinel_db)) as conn:
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                ticker TEXT,
                side TEXT,
                entry_price INTEGER,
                outcome TEXT,
                pnl_cents INTEGER,
                category TEXT,
                confidence REAL,
                settled_at TEXT
            )
            """
        )
        # 4 losses in 'crypto'
        for i in range(4):
            conn.execute(
                "INSERT INTO trades (timestamp, ticker, side, entry_price, outcome, pnl_cents, category, confidence) "
                "VALUES (?, ?, 'yes', 50, 'loss', -50, 'crypto', 70.0)",
                (now_iso, f"KXCRYPTO-TEST-{i}"),
            )
        conn.commit()

    sup = LLMSupervisor(
        config={
            "auto_remediation_enabled": True,
            "min_category_sample_size": 4,
            "cold_category_win_rate_threshold": 40.0,
            "cold_category_throttle_multiplier": 0.50,
        },
        project_root=temp_project,
    )
    report = sup.run_audit()

    assert any(r["type"] == "category_throttle" and r["category"] == "crypto" for r in report["auto_remediations"])
    assert any(f["category"] == "decision_quality" and "crypto" in f["title"].lower() for f in report["findings"])


def test_audit_underdog_approvals(temp_project):
    """Verify sub-30¢ underdog approvals with 0 evidence are flagged as warning."""
    data_dir = temp_project / "data"
    llm_db = data_dir / "central_llm_controller.db"

    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(str(llm_db)) as conn:
        conn.execute(
            """
            CREATE TABLE llm_decisions (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                bot_name TEXT,
                ticker TEXT,
                decision TEXT,
                quant_confidence REAL,
                llm_confidence REAL,
                request_json TEXT,
                response_json TEXT
            )
            """
        )
        req_payload = json.dumps({
            "suggested_price": 22.0,
            "evidence_quality": 0.0,
            "category": "sports",
        })
        conn.execute(
            "INSERT INTO llm_decisions (timestamp, bot_name, ticker, decision, quant_confidence, request_json) "
            "VALUES (?, 'vanguard', 'KXMLB-UNDERDOG-1', 'approve', 82.0, ?)",
            (now_iso, req_payload),
        )
        conn.commit()

    sup = LLMSupervisor(project_root=temp_project)
    report = sup.run_audit()

    assert any(f["ticker"] == "KXMLB-UNDERDOG-1" for f in report["findings"])
    assert any("Underdog Approval" in f["title"] for f in report["findings"])


def test_report_persistence(temp_project):
    """Verify audit reports are saved to JSON and recorded in SQLite database."""
    sup = LLMSupervisor(project_root=temp_project)
    report = sup.run_audit()

    # Check JSON
    latest_file = temp_project / "data" / "supervisor_latest.json"
    assert latest_file.exists()
    with open(latest_file, "r", encoding="utf-8") as fh:
        saved_json = json.load(fh)
    assert saved_json["health_grade"] == report["health_grade"]

    # Check SQLite
    with sqlite3.connect(str(sup._db_path)) as conn:
        row = conn.execute("SELECT health_grade, score_pct FROM supervisor_audits ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert row[0] == report["health_grade"]


def test_supervisor_background_lifecycle(temp_project):
    """Verify start and stop methods handle thread lifecycle safely."""
    sup = LLMSupervisor(config={"interval_minutes": 1}, project_root=temp_project)
    sup.start()
    assert sup._running is True
    assert sup._thread is not None
    assert sup._thread.is_alive()

    # Calling start again shouldn't spawn duplicate threads
    t1 = sup._thread
    sup.start()
    assert sup._thread == t1

    sup.stop()
    assert sup._running is False


def test_audit_rate_limit_auto_remediation(temp_project):
    """Verify that >50 HTTP 429 events triggers autonomous rate limit remediation."""
    logs_dir = temp_project / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    swarm_log = logs_dir / "swarm.log"

    # Write simulated 429 log events
    with open(swarm_log, "w", encoding="utf-8") as fh:
        for i in range(60):
            fh.write(f"2026-09-02 21:46:{i:02d} | WARNING  | oracle | kalshi_agent.kalshi_client | Rate-limited (429). Backing off 2.0s …\n")

    sup = LLMSupervisor(config={"auto_remediation_enabled": True}, project_root=temp_project)
    report = sup.run_audit()

    # Verify auto-remediation was triggered and recorded
    assert any(r["type"] == "rate_limit_throttle" for r in report["auto_remediations"])
    remediation = next(r for r in report["auto_remediations"] if r["type"] == "rate_limit_throttle")
    assert remediation["events_detected"] >= 60

    # Verify finding reflects the active remediation
    rate_finding = next(f for f in report["findings"] if f["category"] == "system_health")
    assert "Auto-tuned rate throttle" in rate_finding["auto_action_taken"]
    assert rate_finding["severity"] == "info"

    # Verify runtime overrides JSON was generated
    overrides_file = temp_project / "data" / "runtime_overrides.json"
    assert overrides_file.exists()
    with open(overrides_file, "r", encoding="utf-8") as fh:
        overrides = json.load(fh)
    assert overrides["rate_limit_per_second"] == 2.5
    assert overrides["recent_trade_seed_top_tickers"] == 35
