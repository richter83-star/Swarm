"""
Tests for Kalshi Swarm Dashboard Console & Management Backend
"""

import json
import pytest
from pathlib import Path
from dashboard_new.server import app, PROJECT_ROOT


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_index_page(client):
    """Test dashboard index HTML renders properly."""
    res = client.get("/")
    assert res.status_code == 200
    html = res.data.decode("utf-8")
    assert "KALSHI" in html
    assert "SWARM" in html
    assert "tab-console" in html
    assert "console-terminal-screen" in html


def test_api_status(client):
    """Test /api/status endpoint returns expected structure."""
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "portfolio_cents" in data
    assert "portfolio_change_pct" in data
    assert "bots" in data
    for bot in ("sentinel", "oracle", "pulse", "vanguard"):
        assert bot in data["bots"]


def test_api_swarm_status(client):
    """Test /api/swarm/status returns swarm process info and trading mode."""
    res = client.get("/api/swarm/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "running" in data
    assert "mode" in data
    assert data["mode"] in ("demo", "live")
    assert "provider" in data


def test_api_swarm_exec_help(client):
    """Test /api/swarm/exec with 'help' command."""
    res = client.post("/api/swarm/exec", json={"command": "help"})
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("ok") is True
    assert "Available Swarm Console Commands" in data.get("output", "")


def test_api_swarm_exec_status(client):
    """Test /api/swarm/exec with 'status' command."""
    res = client.post("/api/swarm/exec", json={"command": "status"})
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("ok") is True
    assert "KALSHI SWARM COMMAND CONSOLE STATUS" in data.get("output", "")


def test_api_swarm_exec_mode_query(client):
    """Test /api/swarm/exec with 'mode' query."""
    res = client.post("/api/swarm/exec", json={"command": "mode"})
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("ok") is True
    assert "Current Trading Mode:" in data.get("output", "")


def test_api_swarm_exec_mode_switch(client):
    """Test /api/swarm/exec switching mode to demo."""
    res = client.post("/api/swarm/exec", json={"command": "mode demo"})
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("ok") is True
    assert data.get("mode") == "demo"
    assert data.get("demo_mode") is True


def test_api_swarm_exec_vacuum(client):
    """Test /api/swarm/exec vacuum command."""
    res = client.post("/api/swarm/exec", json={"command": "vacuum"})
    assert res.status_code == 200
    data = res.get_json()
    assert data.get("ok") is True
    assert "Database VACUUM Routine:" in data.get("output", "")


def test_api_swarm_exec_unknown(client):
    """Test /api/swarm/exec with invalid command returns error."""
    res = client.post("/api/swarm/exec", json={"command": "invalid_xyz_cmd"})
    assert res.status_code == 400
    data = res.get_json()
    assert data.get("ok") is False
    assert "Unknown command" in data.get("error", "")


def test_api_llm(client):
    """Test /api/llm returns structured LLM intelligence report."""
    res = client.get("/api/llm")
    assert res.status_code == 200
    data = res.get_json()
    assert "today" in data
    assert "clean_period" in data
    assert "recent_decisions" in data


def test_api_trades(client):
    """Test /api/trades returns list of recent trade records."""
    res = client.get("/api/trades")
    assert res.status_code == 200
    data = res.get_json()
    assert isinstance(data, list)


def test_api_risk(client):
    """Test /api/risk returns risk matrices and guardrails."""
    res = client.get("/api/risk")
    assert res.status_code == 200
    data = res.get_json()
    assert "bots" in data
    assert "guardrail_progress" in data


def test_api_learning(client):
    """Test /api/learning returns calibration and scorecard."""
    res = client.get("/api/learning")
    assert res.status_code == 200
    data = res.get_json()
    assert "scorecard" in data
    assert "calibration" in data
    assert "categories" in data
    assert "weights" in data


def test_api_system(client):
    """Test /api/system returns telemetry and watchdog checks."""
    res = client.get("/api/system")
    assert res.status_code == 200
    data = res.get_json()
    assert "tavily" in data
    assert "llm_provider" in data
    assert "llm_model" in data
    assert "uptime_seconds" in data
    assert "log_tail" in data


def test_api_config(client):
    """Test /api/config returns parsed yaml and raw config text."""
    res = client.get("/api/config")
    assert res.status_code == 200
    data = res.get_json()
    assert "parsed" in data
    assert "raw" in data
