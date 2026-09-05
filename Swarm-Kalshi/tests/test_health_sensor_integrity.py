"""
tests/test_health_sensor_integrity.py
Comprehensive test suite verifying 4-state health integrity:
PASS / OK, FAIL / CRITICAL, UNKNOWN, DEGRADED, NOT_APPLICABLE.
Ensures failed sensors propagate UNKNOWN / DEGRADED to dependent checks.
"""

import pytest
from unittest.mock import patch, MagicMock
from health_check import (
    check_kalshi_auth_connectivity,
    check_balance_drawdown,
    _aggregate_status,
    _build_summary,
)


def test_kalshi_auth_connectivity_success():
    """When credentials are present and API returns 200, status must be OK."""
    cfg = {
        "api": {
            "demo_mode": True,
            "key_id": "test-key-id",
            "private_key_path": "test-key.pem",
        }
    }
    with patch("health_check.Path.exists", return_value=True), \
         patch("kalshi_agent.kalshi_client.KalshiClient") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.get_balance.return_value = {"balance": 50000}
        mock_client_cls.return_value = mock_instance

        res = check_kalshi_auth_connectivity(cfg)
        assert res.get("status") == "OK"
        assert res.get("authenticated") is True
        assert "authenticated" in res.get("details", "").lower()


def test_kalshi_auth_connectivity_401_degraded():
    """When API returns 401 or auth fails, status must be DEGRADED, never OK."""
    cfg = {
        "api": {
            "demo_mode": True,
            "key_id": "test-key-id",
            "private_key_path": "test-key.pem",
        }
    }
    with patch("health_check.Path.exists", return_value=True), \
         patch("kalshi_agent.kalshi_client.KalshiClient") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.get_balance.side_effect = Exception("HTTP 401 Client Error: Unauthorized for url")
        mock_client_cls.return_value = mock_instance

        res = check_kalshi_auth_connectivity(cfg)
        assert res.get("status") == "DEGRADED"
        assert res.get("authenticated") is False
        assert "401" in res.get("details", "") or "AUTHENTICATION FAILURE" in res.get("details", "")


def test_kalshi_auth_connectivity_network_error_unknown():
    """When network timeout occurs, status must be UNKNOWN."""
    cfg = {
        "api": {
            "demo_mode": True,
            "key_id": "test-key-id",
            "private_key_path": "test-key.pem",
        }
    }
    with patch("health_check.Path.exists", return_value=True), \
         patch("kalshi_agent.kalshi_client.KalshiClient") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.get_balance.side_effect = Exception("ConnectionRefusedError: Network unreachable")
        mock_client_cls.return_value = mock_instance

        res = check_kalshi_auth_connectivity(cfg)
        assert res.get("status") == "UNKNOWN"
        assert "Network unreachable" in res.get("details", "")


def test_balance_drawdown_propagates_unknown_when_auth_degraded():
    """
    CRITICAL INVARIANT:
    A failed sensor is NOT evidence of nominal flight.
    If Kalshi auth is DEGRADED, balance_drawdown must return UNKNOWN — EXTERNAL DATA UNAVAILABLE.
    """
    actions = []
    auth_result = {
        "status": "DEGRADED",
        "details": "HTTP 401 AUTHENTICATION FAILURE on Kalshi demo API",
    }

    res = check_balance_drawdown(actions, auth_result)
    assert res.get("status") == "UNKNOWN"
    assert "UNAVAILABLE" in res.get("details", "") or "UNVERIFIED" in res.get("details", "") or "Kalshi auth" in res.get("details", "")


def test_balance_drawdown_nominal_when_auth_ok():
    """When Kalshi auth is OK and local risk files have nominal balance, return OK."""
    actions = []
    auth_result = {
        "status": "OK",
        "details": "Kalshi API authenticated nominal",
    }

    with patch("health_check._risk_state_path") as mock_rsp, \
         patch("health_check._status_path") as mock_sp:
        mock_file = MagicMock()
        mock_file.exists.return_value = False
        mock_rsp.return_value = mock_file
        mock_sp.return_value = mock_file

        res = check_balance_drawdown(actions, auth_result)
        assert res.get("status") == "OK"


def test_aggregate_status_priorities():
    """Verify priority order: CRITICAL > DEGRADED > WARNING > UNKNOWN > FIXED > OK."""
    # 1. OK only
    checks_ok = {
        "c1": {"status": "OK", "details": "all good"},
        "c2": {"status": "OK", "details": "all good"},
    }
    assert _aggregate_status(checks_ok) == "OK"

    # 2. With UNKNOWN
    checks_unk = {
        "c1": {"status": "OK", "details": "all good"},
        "c2": {"status": "UNKNOWN", "details": "sensor offline"},
    }
    assert _aggregate_status(checks_unk) == "UNKNOWN"

    # 3. With DEGRADED
    checks_deg = {
        "c1": {"status": "OK", "details": "all good"},
        "c2": {"status": "UNKNOWN", "details": "sensor offline"},
        "c3": {"status": "DEGRADED", "details": "auth failed"},
    }
    assert _aggregate_status(checks_deg) == "DEGRADED"

    # 4. With CRITICAL / FAIL
    checks_crit = {
        "c1": {"status": "OK", "details": "all good"},
        "c2": {"status": "DEGRADED", "details": "auth failed"},
        "c3": {"status": "CRITICAL", "details": "corrupted db"},
    }
    assert _aggregate_status(checks_crit) in ("CRITICAL", "FAIL")


def test_build_summary_formatting():
    """Verify summary headline accurately reflects 4-state counts."""
    checks = {
        "c1": {"status": "OK", "details": "nominal"},
        "c2": {"status": "OK", "details": "nominal"},
        "c3": {"status": "DEGRADED", "details": "401 auth"},
        "c4": {"status": "UNKNOWN", "details": "sensor unverified"},
    }
    actions = []
    recs = []
    summary = _build_summary(checks, actions, recs)
    assert "2/4 Checks Nominal" in summary
    assert "1 DEGRADED" in summary
    assert "1 UNKNOWN" in summary
