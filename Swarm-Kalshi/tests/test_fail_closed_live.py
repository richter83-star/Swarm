"""
tests/test_fail_closed_live.py
Unit tests verifying fail-closed live execution when exchange sensors / auth are unverified.
Ensures real capital is never risked when external state cannot be confirmed.
"""

import pytest
from unittest.mock import patch, MagicMock
from kalshi_agent.risk_manager import RiskManager


def _mock_risk_config():
    return {
        "trading": {
            "max_position_cost_cents": 2500,
            "max_daily_loss_cents": 5000,
            "max_open_positions": 5,
        },
        "risk": {
            "max_consecutive_losses": 3,
            "loss_streak_pause_minutes": 30,
            "max_drawdown_pct": 15.0,
        }
    }


def test_risk_manager_sensor_verification_flags():
    """Verify RiskManager tracks exchange sensor verification state."""
    rm = RiskManager(_mock_risk_config())
    assert rm.is_verified is True
    assert rm.exchange_auth_status == "ok"

    # Set degraded
    rm.set_exchange_verified(False, auth_status="degraded")
    assert rm.is_verified is False
    assert rm.exchange_auth_status == "degraded"

    # Update balance with unverified flag
    rm.update_balance(5000, verified=False)
    assert rm.balance_cents == 5000
    assert rm.is_verified is False


def test_risk_manager_fail_closed_live():
    """Live mode trade checks with require_verified_sensors must reject unverified state."""
    rm = RiskManager(_mock_risk_config())
    rm.update_balance(10000, verified=True)
    rm.set_exchange_verified(False, auth_status="degraded")

    # In live mode (require_verified_sensors=True), can_trade must fail closed
    can = rm.can_trade(require_verified_sensors=True)
    assert can is False

    # In demo mode (require_verified_sensors=False), simulated trade may proceed
    can_demo = rm.can_trade(require_verified_sensors=False)
    assert can_demo is True


def test_risk_manager_export_import_sensor_state():
    """RiskManager export_state and import_state must preserve sensor flags."""
    rm = RiskManager(_mock_risk_config())
    rm.set_exchange_verified(False, auth_status="degraded")
    state = rm.export_state()

    assert state.get("balance_verified") is False
    assert state.get("exchange_auth_status") == "degraded"

    rm2 = RiskManager(_mock_risk_config())
    rm2.import_state(state)
    assert rm2.is_verified is False
    assert rm2.exchange_auth_status == "degraded"


def test_bot_runner_live_fail_closed_logic():
    """
    Verify BotRunner fails closed in live mode when sensors are unverified.
    """
    from swarm.bot_runner import BotRunner

    class MockSignal:
        def __init__(self):
            self.bot_name = "pulse"
            self.ticker = "KXTEST-26AUG29-T50"
            self.action = "BUY"
            self.side = "yes"
            self.count = 1
            self.confidence = 0.75
            self.suggested_price = 50
            self.volume_24h = 100
            self.spread_cents = 2

    # Mock environment variables so KalshiClient init succeeds in test
    with patch.dict("os.environ", {"KALSHI_KEY_ID": "mock-key", "KALSHI_API_KEY_ID": "mock-key"}):
        with patch("swarm.bot_runner.KalshiClient"):
            runner = BotRunner(
                "pulse",
                "config/swarm_config.yaml",
                "config/pulse_config.yaml",
            )
            runner.cfg["api"] = runner.cfg.get("api", {})
            runner.cfg["api"]["demo_mode"] = False  # LIVE MODE
            runner._sensors_verified = False
            runner._exchange_auth_status = "degraded"
            runner._last_auth_error = "HTTP 401: Unauthorized"

            signal = MockSignal()

            # In live mode with degraded sensors, _execute_trade must abort immediately
            with patch.object(runner.client, "place_order") as mock_place:
                runner._execute_trade(signal)
                mock_place.assert_not_called()
