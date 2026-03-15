"""
tests/test_balance_manager.py
==============================

Unit tests for BalanceManager — the module that allocates and tracks the
shared Kalshi account balance across bots.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.balance_manager import BalanceManager


DEFAULT_CFG = {
    "budget_allocation": {
        "sentinel": 0.25,
        "oracle": 0.30,
        "pulse": 0.20,
        "vanguard": 0.25,
    },
    "global_daily_loss_limit_cents": 15000,
    "global_exposure_limit_cents": 50000,
}


@pytest.fixture()
def bm():
    mgr = BalanceManager(DEFAULT_CFG)
    mgr.update_total_balance(10_000)  # $100 total
    return mgr


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def test_initial_total_balance(bm):
    assert bm.get_total_balance() == 10_000


def test_allocations_sum_to_one():
    """Allocations that don't sum to 1.0 must be auto-normalized."""
    cfg = {**DEFAULT_CFG, "budget_allocation": {"sentinel": 0.5, "oracle": 0.5, "x": 0.5}}
    mgr = BalanceManager(cfg)
    total = sum(mgr.get_bot_allocation_pct(b) for b in ["sentinel", "oracle", "x"])
    assert abs(total - 1.0) < 0.001


# ---------------------------------------------------------------------------
# get_bot_budget
# ---------------------------------------------------------------------------

def test_bot_budget_reflects_allocation(bm):
    # sentinel = 25% of $100 = $25 = 2500 cents
    assert bm.get_bot_budget("sentinel") == 2500


def test_bot_budget_decreases_after_spend(bm):
    bm.record_spend("sentinel", 500)
    assert bm.get_bot_budget("sentinel") == 2000


def test_bot_budget_never_negative(bm):
    # Force overspend via internal state (spend more than allocated)
    bm._bot_spent["sentinel"] = 99999
    assert bm.get_bot_budget("sentinel") == 0


# ---------------------------------------------------------------------------
# record_spend
# ---------------------------------------------------------------------------

def test_record_spend_within_budget_returns_true(bm):
    assert bm.record_spend("oracle", 100) is True


def test_record_spend_exceeding_budget_returns_false(bm):
    # oracle = 30% of $100 = $30 = 3000 cents; try to spend 5000
    assert bm.record_spend("oracle", 5000) is False


def test_record_spend_zero_or_negative_returns_false(bm):
    assert bm.record_spend("sentinel", 0) is False
    assert bm.record_spend("sentinel", -100) is False


def test_record_spend_accumulates(bm):
    bm.record_spend("pulse", 500)
    bm.record_spend("pulse", 300)
    assert bm.get_total_exposure() == 800


# ---------------------------------------------------------------------------
# record_return
# ---------------------------------------------------------------------------

def test_record_return_restores_budget(bm):
    bm.record_spend("sentinel", 1000)
    bm.record_return("sentinel", 1000)
    assert bm.get_bot_budget("sentinel") == 2500


def test_record_return_does_not_go_below_zero_spent(bm):
    bm.record_return("sentinel", 9999)  # more than was ever spent
    assert bm._bot_spent["sentinel"] == 0


# ---------------------------------------------------------------------------
# P&L tracking
# ---------------------------------------------------------------------------

def test_record_pnl_accumulates(bm):
    bm.record_pnl("sentinel", 200)
    bm.record_pnl("sentinel", -50)
    assert bm.get_total_daily_pnl() == 150


def test_reset_daily_pnl(bm):
    bm.record_pnl("oracle", 500)
    bm.reset_daily_pnl()
    assert bm.get_total_daily_pnl() == 0


# ---------------------------------------------------------------------------
# Global exposure & loss limits
# ---------------------------------------------------------------------------

def test_check_global_exposure_limit_ok(bm):
    bm.record_spend("sentinel", 1000)
    assert bm.check_global_exposure_limit() is True


def test_check_global_exposure_limit_breached(bm):
    # Manually set exposure above limit
    for bot in DEFAULT_CFG["budget_allocation"]:
        bm._bot_spent[bot] = 15000  # 4 bots × $150 = $600 > $500 limit
    assert bm.check_global_exposure_limit() is False


def test_check_global_daily_loss_limit_ok(bm):
    bm.record_pnl("sentinel", -100)
    assert bm.check_global_daily_loss_limit() is True


def test_check_global_daily_loss_limit_breached(bm):
    bm.record_pnl("oracle", -20000)  # -$200 > $150 limit
    assert bm.check_global_daily_loss_limit() is False


# ---------------------------------------------------------------------------
# can_execute_trade — trade guard integration
# ---------------------------------------------------------------------------

def _make_guard(daily_pnl=0, exposure=0, bot_budget=3000):
    return {
        "valid": True,
        "timestamp": "2026-01-01T00:00:00+00:00",
        "limits": {
            "global_daily_loss_limit_cents": 15000,
            "global_exposure_limit_cents": 50000,
        },
        "metrics": {
            "total_daily_pnl_cents": daily_pnl,
            "total_exposure_cents": exposure,
        },
        "bots": {
            "oracle": {"available_budget_cents": bot_budget},
        },
    }


def test_can_execute_trade_approved(bm):
    guard = _make_guard()
    ok, reason = bm.can_execute_trade("oracle", "TICK-A", 500, guard)
    assert ok is True
    assert "approved" in reason


def test_can_execute_trade_rejects_non_positive_notional(bm):
    guard = _make_guard()
    ok, reason = bm.can_execute_trade("oracle", "TICK-A", 0, guard)
    assert ok is False


def test_can_execute_trade_rejects_missing_guard(bm):
    ok, reason = bm.can_execute_trade("oracle", "TICK-A", 500, {})
    assert ok is False


def test_can_execute_trade_rejects_when_daily_loss_hit(bm):
    guard = _make_guard(daily_pnl=-15000)
    ok, reason = bm.can_execute_trade("oracle", "TICK-A", 100, guard)
    assert ok is False
    assert "loss" in reason.lower()


def test_can_execute_trade_rejects_when_exposure_exceeded(bm):
    guard = _make_guard(exposure=49999)
    ok, reason = bm.can_execute_trade("oracle", "TICK-A", 5000, guard)
    assert ok is False
    assert "exposure" in reason.lower()


def test_can_execute_trade_rejects_when_bot_budget_insufficient(bm):
    guard = _make_guard(bot_budget=100)
    ok, reason = bm.can_execute_trade("oracle", "TICK-A", 500, guard)
    assert ok is False
    assert "budget" in reason.lower()


# ---------------------------------------------------------------------------
# status snapshot
# ---------------------------------------------------------------------------

def test_status_contains_expected_keys(bm):
    bm.record_spend("sentinel", 200)
    bm.record_pnl("oracle", 50)
    s = bm.status()
    assert "total_balance_cents" in s
    assert "total_exposure_cents" in s
    assert "total_pnl_cents" in s
    assert "bots" in s
    assert "sentinel" in s["bots"]


def test_status_exposure_matches_spend(bm):
    bm.record_spend("sentinel", 300)
    bm.record_spend("oracle", 200)
    s = bm.status()
    assert s["total_exposure_cents"] == 500
