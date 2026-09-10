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


def test_audit_profit_and_expired_calibration(temp_project):
    """Verify that zero-loss expired trades and profitable bots are not falsely penalized."""
    data_dir = temp_project / "data"
    pulse_db = data_dir / "pulse.db"
    vanguard_db = data_dir / "vanguard.db"

    now_iso = datetime.now(timezone.utc).isoformat()
    # Pulse has 10 expired trades, 0 losses, 0 PnL
    with sqlite3.connect(str(pulse_db)) as conn:
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
        for i in range(10):
            conn.execute(
                "INSERT INTO trades (timestamp, ticker, side, entry_price, outcome, pnl_cents, category, confidence) "
                "VALUES (?, ?, 'yes', 50, 'expired', 0, 'weather', 70.0)",
                (now_iso, f"KXWEATHER-TEST-{i}"),
            )
        conn.commit()

    # Vanguard has 5 wins, 5 losses, but net profit +500 cents (profitable EV strategy)
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
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (timestamp, ticker, side, entry_price, outcome, pnl_cents, category, confidence) "
                "VALUES (?, ?, 'yes', 20, 'win', 200, 'sports', 75.0)",
                (now_iso, f"KXMLB-WIN-{i}"),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO trades (timestamp, ticker, side, entry_price, outcome, pnl_cents, category, confidence) "
                "VALUES (?, ?, 'yes', 20, 'loss', -100, 'sports', 75.0)",
                (now_iso, f"KXMLB-LOSS-{i}"),
            )
        conn.commit()

    sup = LLMSupervisor(project_root=temp_project)
    report = sup.run_audit()

    # Pulse should NOT receive a low win rate warning (0 losses)
    assert not any("Low Historical Win Rate on Pulse" in f["title"] for f in report["findings"])
    # Weather should NOT be throttled as a failing category (0 losses, 0 PnL)
    assert not any("Sub-par Category Performance: 'weather'" in f["title"] for f in report["findings"])
    # Vanguard is profitable (+500c) with 50% win rate, should have no warnings
    assert not any("Low Historical Win Rate on Vanguard" in f["title"] for f in report["findings"])
    # Net PnL is tracked
    assert report["total_pnl_cents"] == 500
    assert report["health_grade"] in ("A+", "A")


def test_supervisor_auto_quarantines_severe_drag(temp_project):
    """Verify that bots with severe win rate drag (<20% across >=5 decided trades) are auto-quarantined."""
    data_dir = temp_project / "data"
    pulse_db = data_dir / "pulse.db"

    now_iso = datetime.now(timezone.utc).isoformat()
    # Pulse has 10 losses, 0 wins, negative PnL (-1500 cents)
    with sqlite3.connect(str(pulse_db)) as conn:
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
        for i in range(10):
            conn.execute(
                "INSERT INTO trades (timestamp, ticker, side, entry_price, outcome, pnl_cents, category, confidence) "
                "VALUES (?, ?, 'no', 50, 'loss', -150, 'weather', 62.0)",
                (now_iso, f"KXWEATHER-FAIL-{i}"),
            )
        conn.commit()

    sup = LLMSupervisor(project_root=temp_project)
    report = sup.run_audit()

    # 1. Finding is registered with critical severity
    quarantine_finding = next(
        (f for f in report["findings"] if "Autonomous Quarantine Active on Pulse" in f["title"]),
        None,
    )
    assert quarantine_finding is not None
    assert quarantine_finding["severity"] == "critical"
    assert "Auto-quarantined Pulse" in (quarantine_finding["auto_action_taken"] or "")

    # 2. Remediation logged
    quarantine_remediation = next(
        (r for r in report["auto_remediations"] if r.get("type") == "bot_quarantine" and r.get("bot_name") == "pulse"),
        None,
    )
    assert quarantine_remediation is not None
    assert quarantine_remediation["win_rate"] == 0.0
    assert quarantine_remediation["pnl_cents"] == -1500

    # 3. Synchronized to runtime_overrides.json
    overrides_file = data_dir / "runtime_overrides.json"
    assert overrides_file.exists()
    with open(overrides_file, "r", encoding="utf-8") as fh:
        overrides = json.load(fh)

    assert "pulse" in overrides.get("quarantined_bots", [])
    assert overrides["bot_overrides"]["pulse"]["paused"] is True
    assert overrides["category_throttles"]["weather"] == 0.50


def test_bot_runner_honors_quarantine_and_threshold_elevation(temp_project):
    """Verify that BotRunner dynamically reads runtime_overrides.json and honors quarantine/elevations."""
    from unittest.mock import patch, MagicMock
    from swarm.bot_runner import BotRunner

    data_dir = temp_project / "data"
    overrides_file = data_dir / "runtime_overrides.json"
    overrides_file.write_text(
        json.dumps({
            "quarantined_bots": ["pulse"],
            "bot_overrides": {
                "pulse": {
                    "paused": True,
                    "min_confidence_threshold": 75.0,
                    "reason": "Test severe drag quarantine",
                }
            },
            "category_throttles": {
                "weather": 0.50,
                "crypto": 0.0,
            }
        }),
        encoding="utf-8"
    )

    with patch.dict("os.environ", {"KALSHI_KEY_ID": "mock-key", "KALSHI_API_KEY_ID": "mock-key"}), \
         patch("swarm.bot_runner.KalshiClient"):
        runner = BotRunner(
            "pulse",
            "config/swarm_config.yaml",
            "config/pulse_config.yaml",
            project_root=str(temp_project),
        )
        assert runner._quarantined_by_supervisor is True
        assert "Test severe drag quarantine" in runner._quarantine_reason
        assert runner.analysis.cfg["min_confidence_threshold"] == 75.0
        assert runner._category_throttles["weather"] == 0.50
        assert runner._category_throttles["crypto"] == 0.0


