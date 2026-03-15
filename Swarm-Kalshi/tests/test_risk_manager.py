"""
tests/test_risk_manager.py
===========================
Unit tests for RiskManager — all five can_trade guards, position sizing,
streak/drawdown logic, and state serialisation round-trip.

All tests are pure-Python: no network, no database, no config file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kalshi_agent.risk_manager import RiskManager, DailyPnL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_rm(**overrides) -> RiskManager:
    """Build a RiskManager with safe defaults, overrideable by kwargs."""
    cfg = {
        "min_balance_cents": 1000,
        "daily_loss_limit_cents": 500,
        "max_trades_per_day": 10,
        "max_open_positions": 5,
        "max_drawdown_pct": 0.20,          # 20% as a decimal fraction
        "drawdown_pause_cooldown_minutes": 60,
        "max_position_pct": 0.05,          # 5% as a decimal fraction
        "loss_streak_threshold": 3,
        "loss_streak_size_multiplier": 0.5,
    }
    cfg.update(overrides)
    rm = RiskManager(config=cfg)
    rm.update_balance(10000)  # $100 starting balance
    return rm


# ---------------------------------------------------------------------------
# DailyPnL
# ---------------------------------------------------------------------------

class TestDailyPnL:
    def test_initial_state(self):
        d = DailyPnL()
        assert d.gross_pnl_cents == 0
        assert d.trades_today == 0
        assert d.wins == 0
        assert d.losses == 0

    def test_reset_if_same_day_noop(self):
        d = DailyPnL()
        d.gross_pnl_cents = 200
        d.reset_if_new_day()
        assert d.gross_pnl_cents == 200  # same day, no reset


# ---------------------------------------------------------------------------
# can_trade — minimum balance guard
# ---------------------------------------------------------------------------

class TestCanTradeMinBalance:
    def test_below_min_balance_blocks(self):
        rm = make_rm(min_balance_cents=5000)
        rm.update_balance(4999)
        assert not rm.can_trade()

    def test_at_min_balance_allows(self):
        # Disable drawdown guard (set to 100%) so only min_balance is tested
        rm = make_rm(min_balance_cents=1000, max_drawdown_pct=1.0)
        rm.update_balance(1000)
        assert rm.can_trade()

    def test_above_min_balance_allows(self):
        rm = make_rm(min_balance_cents=1000, max_drawdown_pct=1.0)
        rm.update_balance(2000)
        assert rm.can_trade()


# ---------------------------------------------------------------------------
# can_trade — daily loss limit
# ---------------------------------------------------------------------------

class TestCanTradeDailyLoss:
    def test_daily_loss_exceeded_blocks(self):
        rm = make_rm(daily_loss_limit_cents=200)
        rm.record_outcome(-300)  # loss of 300¢ > limit of 200¢
        assert not rm.can_trade()

    def test_daily_loss_at_limit_blocks(self):
        rm = make_rm(daily_loss_limit_cents=200)
        rm.record_outcome(-200)  # exactly at limit
        assert not rm.can_trade()

    def test_below_daily_loss_allows(self):
        rm = make_rm(daily_loss_limit_cents=500)
        rm.record_outcome(-100)
        assert rm.can_trade()


# ---------------------------------------------------------------------------
# can_trade — max trades per day
# ---------------------------------------------------------------------------

class TestCanTradeMaxTrades:
    def test_max_trades_reached_blocks(self):
        rm = make_rm(max_trades_per_day=3)
        rm.daily.trades_today = 3
        assert not rm.can_trade()

    def test_below_max_trades_allows(self):
        rm = make_rm(max_trades_per_day=10)
        rm.daily.trades_today = 9
        assert rm.can_trade()

    def test_record_outcome_increments_trade_count(self):
        rm = make_rm(max_trades_per_day=2)
        rm.record_outcome(10)
        rm.record_outcome(10)
        assert not rm.can_trade()  # 2 trades == limit


# ---------------------------------------------------------------------------
# can_trade — max open positions
# ---------------------------------------------------------------------------

class TestCanTradeOpenPositions:
    def test_max_open_positions_blocks(self):
        rm = make_rm(max_open_positions=3)
        rm.update_open_positions(3)
        assert not rm.can_trade()

    def test_below_max_open_positions_allows(self):
        rm = make_rm(max_open_positions=5)
        rm.update_open_positions(4)
        assert rm.can_trade()

    def test_zero_positions_allows(self):
        rm = make_rm(max_open_positions=5)
        rm.update_open_positions(0)
        assert rm.can_trade()


# ---------------------------------------------------------------------------
# can_trade — drawdown guard
# ---------------------------------------------------------------------------

class TestCanTradeDrawdown:
    def test_drawdown_exceeded_blocks(self):
        rm = make_rm(max_drawdown_pct=0.10, drawdown_pause_cooldown_minutes=0)
        rm.update_balance(10000)   # peak = 10000
        rm.update_balance(8999)    # 10.01% drawdown > 10% limit
        assert not rm.can_trade()

    def test_drawdown_within_limit_allows(self):
        rm = make_rm(max_drawdown_pct=0.20)
        rm.update_balance(10000)
        rm.update_balance(8500)    # 15% drawdown < 20% limit
        assert rm.can_trade()

    def test_no_peak_no_drawdown_block(self):
        """If peak is 0, drawdown check is skipped."""
        rm = RiskManager(config={
            "min_balance_cents": 0,
            "daily_loss_limit_cents": 99999,
            "max_trades_per_day": 100,
            "max_open_positions": 100,
            "max_drawdown_pct": 0.10,
        })
        # Don't call update_balance → peak stays 0
        assert rm.can_trade()


# ---------------------------------------------------------------------------
# record_outcome and consecutive losses
# ---------------------------------------------------------------------------

class TestRecordOutcome:
    def test_win_resets_consecutive_losses(self):
        rm = make_rm()
        rm.record_outcome(-50)
        rm.record_outcome(-50)
        assert rm._consecutive_losses == 2
        rm.record_outcome(100)
        assert rm._consecutive_losses == 0

    def test_loss_increments_streak(self):
        rm = make_rm()
        rm.record_outcome(-10)
        rm.record_outcome(-20)
        assert rm._consecutive_losses == 2

    def test_breakeven_resets_streak(self):
        rm = make_rm()
        rm.record_outcome(-10)
        assert rm._consecutive_losses == 1
        rm.record_outcome(0)   # breakeven resets streak
        assert rm._consecutive_losses == 0

    def test_daily_pnl_accumulates(self):
        rm = make_rm()
        rm.record_outcome(100)
        rm.record_outcome(-30)
        assert rm.daily.gross_pnl_cents == 70

    def test_daily_trade_count_increments(self):
        rm = make_rm()
        rm.record_outcome(50)
        rm.record_outcome(-20)
        assert rm.daily.trades_today == 2

    def test_win_increments_wins(self):
        rm = make_rm()
        rm.record_outcome(50)
        assert rm.daily.wins == 1
        assert rm.daily.losses == 0

    def test_loss_increments_losses(self):
        rm = make_rm()
        rm.record_outcome(-50)
        assert rm.daily.losses == 1
        assert rm.daily.wins == 0


# ---------------------------------------------------------------------------
# update_balance — peak tracking
# ---------------------------------------------------------------------------

class TestUpdateBalance:
    def test_peak_tracks_high_water_mark(self):
        rm = make_rm()
        rm.update_balance(10000)
        rm.update_balance(15000)
        assert rm._peak_balance_cents == 15000
        rm.update_balance(8000)
        assert rm._peak_balance_cents == 15000  # peak not reduced on balance drop

    def test_balance_updated(self):
        rm = make_rm()
        rm.update_balance(7777)
        assert rm._current_balance_cents == 7777

    def test_initial_peak_zero(self):
        rm = RiskManager(config={})
        assert rm._peak_balance_cents == 0


# ---------------------------------------------------------------------------
# position_size
# ---------------------------------------------------------------------------

class TestPositionSize:
    def test_high_confidence_gives_bigger_size(self):
        rm = make_rm(max_position_pct=0.10)
        rm.update_balance(10000)
        size_high = rm.position_size(confidence=90, price_cents=50)
        size_low = rm.position_size(confidence=60, price_cents=50)
        assert size_high >= size_low

    def test_loss_streak_reduces_size(self):
        rm = make_rm(max_position_pct=0.10, loss_streak_threshold=3, loss_streak_size_multiplier=0.5)
        rm.update_balance(10000)
        size_no_streak = rm.position_size(confidence=80, price_cents=50)
        # Trigger streak by recording 3 losses
        for _ in range(3):
            rm.record_outcome(-50)
        size_with_streak = rm.position_size(confidence=80, price_cents=50)
        assert size_with_streak <= size_no_streak

    def test_returns_positive_int(self):
        rm = make_rm()
        rm.update_balance(10000)
        size = rm.position_size(confidence=75, price_cents=50)
        assert isinstance(size, int)
        assert size >= 1

    def test_high_price_reduces_contract_count(self):
        rm = make_rm(max_position_pct=0.05)
        rm.update_balance(10000)
        size_cheap = rm.position_size(confidence=80, price_cents=10)
        size_expensive = rm.position_size(confidence=80, price_cents=90)
        assert size_cheap >= size_expensive

    def test_zero_price_returns_one(self):
        """Zero/negative price falls back to returning 1 contract."""
        rm = make_rm()
        rm.update_balance(10000)
        assert rm.position_size(confidence=80, price_cents=0) == 1


# ---------------------------------------------------------------------------
# export_state / import_state round-trip
# ---------------------------------------------------------------------------

class TestStateSerialisation:
    def test_round_trip_preserves_balance(self):
        rm = make_rm()
        rm.update_balance(12345)
        state = rm.export_state()
        rm2 = make_rm()
        rm2.import_state(state)
        assert rm2._current_balance_cents == 12345

    def test_round_trip_preserves_peak_balance(self):
        rm = make_rm()
        rm.update_balance(15000)  # new peak above the 10000 set by make_rm
        rm.update_balance(5000)   # drop below peak
        state = rm.export_state()
        rm2 = make_rm()
        rm2.import_state(state)
        assert rm2._peak_balance_cents == 15000

    def test_round_trip_preserves_consecutive_losses(self):
        rm = make_rm()
        rm.record_outcome(-10)
        rm.record_outcome(-10)
        state = rm.export_state()
        rm2 = make_rm()
        rm2.import_state(state)
        assert rm2._consecutive_losses == 2

    def test_round_trip_preserves_daily_pnl(self):
        rm = make_rm()
        rm.record_outcome(200)
        rm.record_outcome(-50)
        state = rm.export_state()
        rm2 = make_rm()
        rm2.import_state(state)
        assert rm2.daily.gross_pnl_cents == 150

    def test_export_returns_dict(self):
        rm = make_rm()
        state = rm.export_state()
        assert isinstance(state, dict)
        assert "daily" in state
        assert "consecutive_losses" in state

    def test_import_from_empty_dict_does_not_crash(self):
        rm = make_rm()
        rm.import_state({})  # Should not raise


# ---------------------------------------------------------------------------
# status() diagnostic snapshot
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_returns_dict(self):
        rm = make_rm()
        s = rm.status()
        assert isinstance(s, dict)

    def test_status_contains_balance(self):
        rm = make_rm()
        rm.update_balance(9876)
        s = rm.status()
        assert s["balance_cents"] == 9876

    def test_status_contains_consecutive_losses(self):
        rm = make_rm()
        rm.record_outcome(-10)
        s = rm.status()
        assert s["consecutive_losses"] == 1

    def test_status_drawdown_pct_zero_when_at_peak(self):
        rm = make_rm()
        rm.update_balance(10000)
        s = rm.status()
        assert s["drawdown_pct"] == 0.0

    def test_status_drawdown_pct_positive_when_below_peak(self):
        rm = make_rm()
        rm.update_balance(10000)
        rm.update_balance(8000)
        s = rm.status()
        assert s["drawdown_pct"] > 0.0
