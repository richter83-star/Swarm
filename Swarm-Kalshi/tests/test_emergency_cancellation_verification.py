"""
tests/test_emergency_cancellation_verification.py
=================================================
Automated verification tests for:
1. Emergency order cancellation verified outcome vs unverified auth failure.
2. /api/kill verified exchange cancellation outcome.
3. Drawdown baseline semantics (N/A when peak <= 0).
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard_new.server import app, verify_and_cancel_exchange_orders


def test_verify_and_cancel_exchange_orders_missing_credentials(tmp_path):
    with patch("dashboard_new.server.load_yaml", return_value={"api": {"key_id": ""}}):
        report = verify_and_cancel_exchange_orders()
        assert report["ok"] is False
        assert report["status"] == "UNVERIFIED_AUTH_FAILURE"
        assert report["exchange_verified"] is False
        assert "Cannot query or cancel orders on exchange" in report["details"]


def test_verify_and_cancel_exchange_orders_401_auth_failure():
    mock_client = MagicMock()
    mock_client.get_orders.side_effect = Exception("HTTP 401: Unauthorized")

    with patch("dashboard_new.server.load_yaml", return_value={"api": {"key_id": "test-key", "private_key_path": "test.key"}}), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("kalshi_agent.kalshi_client.KalshiClient", return_value=mock_client):
        report = verify_and_cancel_exchange_orders()
        assert report["ok"] is False
        assert report["status"] == "UNVERIFIED_AUTH_FAILURE"
        assert report["exchange_verified"] is False
        assert "Exchange cancellation UNVERIFIED" in report["details"]


def test_verify_and_cancel_exchange_orders_verified_cleared():
    mock_client = MagicMock()
    # First call returns 2 resting orders, second verification call returns 0 resting orders
    mock_client.get_orders.side_effect = [
        [{"order_id": "ord-1"}, {"order_id": "ord-2"}],
        [],
    ]

    with patch("dashboard_new.server.load_yaml", return_value={"api": {"key_id": "test-key", "private_key_path": "test.key"}}), \
         patch("pathlib.Path.exists", return_value=True), \
         patch("kalshi_agent.kalshi_client.KalshiClient", return_value=mock_client):
        report = verify_and_cancel_exchange_orders()
        assert report["ok"] is True
        assert report["status"] == "VERIFIED_CLEARED"
        assert report["exchange_verified"] is True
        assert report["orders_found"] == 2
        assert report["orders_cancelled"] == 2
        assert report["remaining_orders"] == 0
        assert mock_client.cancel_order.call_count == 2


def test_api_kill_endpoint_returns_cancellation_report():
    with app.test_client() as client:
        # Invalid confirm
        res = client.post("/api/kill", json={"confirm": "NO"})
        assert res.status_code == 400

        # Valid confirm with mock
        with patch("dashboard_new.server.verify_and_cancel_exchange_orders", return_value={
            "ok": True,
            "status": "VERIFIED_CLEARED",
            "exchange_verified": True,
            "orders_found": 0,
            "orders_cancelled": 0,
            "remaining_orders": 0,
            "details": "Verified 0 resting orders on Kalshi exchange (exchange clean)."
        }), patch("dashboard_new.server._get_swarm_processes", return_value=[]):
            res = client.post("/api/kill", json={"confirm": "KILL"})
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            assert data["cancellation"]["status"] == "VERIFIED_CLEARED"
            assert data["cancellation"]["exchange_verified"] is True


def test_api_risk_drawdown_none_without_peak():
    with app.test_client() as client:
        with patch("dashboard_new.server.read_risk_state", return_value={
            "balance_cents": 0,
            "peak_balance_cents": 0,
            "daily_pnl_cents": 0,
            "daily_trades": 0,
            "pause_until": None,
        }):
            res = client.get("/api/risk")
            assert res.status_code == 200
            data = res.get_json()
            # For each bot, drawdown_pct should be None (null in json)
            for bot, b in data["bots"].items():
                assert b["drawdown_pct"] is None
