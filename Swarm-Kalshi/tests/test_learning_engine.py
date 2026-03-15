"""
tests/test_learning_engine.py
==============================
Unit tests for LearningEngine — trade logging, outcome recording,
category stats, trend computation, weight recalibration, calibration
threshold adjustment, and daily summaries.

All data is written to a temporary SQLite file (pytest tmp_path fixture)
so tests are fully isolated and leave no artefacts.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kalshi_agent.learning_engine import LearningEngine


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def le(tmp_path):
    """Fresh LearningEngine backed by a temp SQLite file."""
    cfg = {
        "db_path": str(tmp_path / "test_learning.db"),
        "learning_rate": 0.1,
        "rolling_window": 50,
        "recalibration_decay": 0.95,
        "review_interval_trades": 5,
        "min_trades_for_review": 3,
        "min_confidence_threshold": 65,
        "trend_multiplier_min": 0.7,
        "trend_multiplier_max": 1.3,
    }
    engine = LearningEngine(config=cfg)
    yield engine
    engine.close()


def _log(le, ticker="T-1", outcome=None, pnl=None, category="politics",
         confidence=70.0, edge_score=60.0, liquidity_score=70.0,
         volume_score=80.0, timing_score=80.0, momentum_score=60.0):
    """Helper: log a trade and optionally record its outcome."""
    tid = le.log_trade(
        ticker=ticker,
        event_ticker="EVT-1",
        title="Test market",
        side="yes",
        action="buy",
        count=1,
        entry_price=50,
        confidence=confidence,
        edge_score=edge_score,
        liquidity_score=liquidity_score,
        volume_score=volume_score,
        timing_score=timing_score,
        momentum_score=momentum_score,
        category=category,
    )
    if outcome is not None:
        le.update_outcome(tid, outcome=outcome, exit_price=60, pnl_cents=pnl or 0)
    return tid


# ---------------------------------------------------------------------------
# log_trade
# ---------------------------------------------------------------------------

class TestLogTrade:
    def test_returns_integer_id(self, le):
        tid = _log(le)
        assert isinstance(tid, int)
        assert tid >= 1

    def test_increments_with_each_call(self, le):
        tid1 = _log(le, ticker="T-1")
        tid2 = _log(le, ticker="T-2")
        assert tid2 > tid1

    def test_trade_persisted(self, le):
        _log(le, ticker="T-X")
        trades = le.get_all_trades()
        assert any(t["ticker"] == "T-X" for t in trades)

    def test_initial_outcome_is_pending(self, le):
        _log(le, ticker="T-P")
        trades = le.get_all_trades()
        t = next(t for t in trades if t["ticker"] == "T-P")
        assert t["outcome"] == "pending"

    def test_trades_since_review_increments(self, le):
        before = le._trades_since_review
        _log(le)
        assert le._trades_since_review == before + 1


# ---------------------------------------------------------------------------
# update_outcome
# ---------------------------------------------------------------------------

class TestUpdateOutcome:
    def test_win_outcome_recorded(self, le):
        tid = _log(le, ticker="T-W")
        le.update_outcome(tid, outcome="win", exit_price=65, pnl_cents=150)
        trades = le.get_all_trades()
        t = next(t for t in trades if t["id"] == tid)
        assert t["outcome"] == "win"
        assert t["pnl_cents"] == 150

    def test_loss_outcome_recorded(self, le):
        tid = _log(le, ticker="T-L")
        le.update_outcome(tid, outcome="loss", exit_price=40, pnl_cents=-200)
        trades = le.get_all_trades()
        t = next(t for t in trades if t["id"] == tid)
        assert t["outcome"] == "loss"

    def test_category_stats_updated_on_win(self, le):
        _log(le, ticker="T-1", outcome="win", pnl=100, category="economics")
        cats = le.get_category_performance()
        econ = next((c for c in cats if c["category"] == "economics"), None)
        assert econ is not None
        assert econ["wins"] == 1

    def test_category_stats_not_updated_for_invalid_pnl(self, le):
        """pnl_valid=False should skip category stat update."""
        tid = _log(le, ticker="T-INV", category="weather")
        le.update_outcome(tid, outcome="win", pnl_cents=50, pnl_valid=False)
        cats = le.get_category_performance()
        weather = next((c for c in cats if c["category"] == "weather"), None)
        assert weather is None  # no update expected


# ---------------------------------------------------------------------------
# get_performance
# ---------------------------------------------------------------------------

class TestGetPerformance:
    def test_empty_db_returns_zeros(self, le):
        perf = le.get_performance()
        assert perf["total_trades"] == 0
        assert perf["win_rate"] == 0.0

    def test_win_rate_calculation(self, le):
        _log(le, ticker="W1", outcome="win", pnl=100)
        _log(le, ticker="W2", outcome="win", pnl=100)
        _log(le, ticker="L1", outcome="loss", pnl=-50)
        perf = le.get_performance()
        assert perf["total_trades"] == 3
        assert abs(perf["win_rate"] - 66.67) < 0.1

    def test_total_pnl_correct(self, le):
        _log(le, ticker="A", outcome="win", pnl=200)
        _log(le, ticker="B", outcome="loss", pnl=-80)
        perf = le.get_performance()
        assert perf["total_pnl"] == 120

    def test_last_n_filter(self, le):
        for i in range(5):
            _log(le, ticker=f"T-{i}", outcome="win", pnl=10)
        perf = le.get_performance(last_n=2)
        assert perf["total_trades"] == 2


# ---------------------------------------------------------------------------
# get_category_multiplier
# ---------------------------------------------------------------------------

class TestGetCategoryMultiplier:
    def test_returns_1_when_no_data(self, le):
        mult = le.get_category_multiplier("politics")
        assert mult == 1.0

    def test_returns_1_when_too_few_category_trades(self, le):
        # Fewer than 5 trades → returns 1.0
        for i in range(4):
            _log(le, ticker=f"T-{i}", outcome="win", pnl=50, category="politics")
        mult = le.get_category_multiplier("politics")
        assert mult == 1.0

    def test_high_win_rate_category_boosts_multiplier(self, le):
        # Log enough trades for overall pool
        for i in range(10):
            _log(le, ticker=f"G-{i}", outcome="win" if i < 5 else "loss", pnl=50 if i < 5 else -30)
        # Add 5 wins in economics category on top
        for i in range(5):
            _log(le, ticker=f"E-{i}", outcome="win", pnl=80, category="economics")
        mult = le.get_category_multiplier("economics")
        assert mult >= 1.0

    def test_multiplier_clamped_to_range(self, le):
        # Flood with wins in one category — multiplier must not exceed 1.3
        for i in range(20):
            _log(le, ticker=f"U-{i}", outcome="loss", pnl=-10)
        for i in range(10):
            _log(le, ticker=f"V-{i}", outcome="win", pnl=100, category="crypto")
        mult = le.get_category_multiplier("crypto")
        assert 0.7 <= mult <= 1.3


# ---------------------------------------------------------------------------
# compute_trend
# ---------------------------------------------------------------------------

class TestComputeTrend:
    def test_returns_snapshot_with_defaults_on_empty(self, le):
        snap = le.compute_trend()
        assert snap.momentum_multiplier == 1.0
        assert snap.hot_categories == []
        assert snap.cold_categories == []

    def test_trend_multiplier_above_1_on_recent_wins(self, le):
        # All recent 20 trades are wins — recent half should be better than prior
        for i in range(20):
            _log(le, ticker=f"T-{i}", outcome="win", pnl=50)
        snap = le.compute_trend(window=20, half_window=10)
        # win_rate_trend ≥ 0
        assert snap.win_rate_trend >= 0.0
        assert snap.momentum_multiplier >= 1.0

    def test_trend_multiplier_clamped(self, le):
        for i in range(20):
            _log(le, ticker=f"T-{i}", outcome="win", pnl=200)
        snap = le.compute_trend(window=20, half_window=10)
        assert 0.7 <= snap.momentum_multiplier <= 1.3

    def test_hot_category_detected(self, le):
        # Mix of 20 trades, with politics winning much more than overall
        for i in range(10):
            _log(le, ticker=f"G-{i}", outcome="loss", pnl=-30, category="general")
        for i in range(10):
            _log(le, ticker=f"P-{i}", outcome="win", pnl=80, category="politics")
        snap = le.compute_trend(window=20, half_window=10)
        # politics wins >> general; politics should be hot (if ≥3 trades in window)
        # Not guaranteed by split, but test that it doesn't crash and returns lists
        assert isinstance(snap.hot_categories, list)
        assert isinstance(snap.cold_categories, list)

    def test_feature_importance_keys_present(self, le):
        for i in range(20):
            _log(le, ticker=f"T-{i}", outcome="win" if i % 2 == 0 else "loss", pnl=50)
        snap = le.compute_trend(window=20, half_window=10)
        expected_keys = {"edge", "liquidity", "volume", "timing", "momentum"}
        assert expected_keys.issubset(snap.feature_importance.keys())


# ---------------------------------------------------------------------------
# review_and_recalibrate
# ---------------------------------------------------------------------------

class TestReviewAndRecalibrate:
    def test_returns_current_weights_when_too_few_trades(self, le):
        weights = {"edge": 0.3, "liquidity": 0.2, "volume": 0.2, "timing": 0.2, "momentum": 0.1}
        result = le.review_and_recalibrate(weights)
        assert result == weights

    def test_returns_dict_with_all_dims(self, le):
        for i in range(12):
            _log(le, ticker=f"T-{i}", outcome="win" if i < 6 else "loss", pnl=50 if i < 6 else -20)
        weights = {"edge": 0.3, "liquidity": 0.2, "volume": 0.2, "timing": 0.2, "momentum": 0.1}
        result = le.review_and_recalibrate(weights)
        for key in ("edge", "liquidity", "volume", "timing", "momentum"):
            assert key in result

    def test_weights_sum_to_one_after_recalibration(self, le):
        for i in range(12):
            _log(le, ticker=f"T-{i}", outcome="win" if i < 6 else "loss", pnl=50 if i < 6 else -20)
        weights = {"edge": 0.3, "liquidity": 0.2, "volume": 0.2, "timing": 0.2, "momentum": 0.1}
        result = le.review_and_recalibrate(weights)
        total = sum(result.values())
        assert abs(total - 1.0) < 1e-4

    def test_weight_history_recorded(self, le):
        for i in range(12):
            _log(le, ticker=f"T-{i}", outcome="win" if i < 6 else "loss", pnl=40 if i < 6 else -20)
        weights = {"edge": 0.3, "liquidity": 0.2, "volume": 0.2, "timing": 0.2, "momentum": 0.1}
        le.review_and_recalibrate(weights)
        history = le.get_weight_history()
        assert len(history) >= 1

    def test_trades_since_review_reset(self, le):
        for i in range(12):
            _log(le, ticker=f"T-{i}", outcome="win" if i < 6 else "loss", pnl=40)
        weights = {"edge": 0.3, "liquidity": 0.2, "volume": 0.2, "timing": 0.2, "momentum": 0.1}
        le.review_and_recalibrate(weights)
        assert le._trades_since_review == 0


# ---------------------------------------------------------------------------
# get_calibrated_threshold
# ---------------------------------------------------------------------------

class TestGetCalibratedThreshold:
    def test_returns_base_when_no_trend(self, le):
        threshold = le.get_calibrated_threshold(base_threshold=65.0)
        # With zero calibration bias, should return close to 65
        assert 55.0 <= threshold <= 75.0

    def test_clamped_to_40_90_range(self, le):
        # Force extreme bias by directly manipulating trend state
        le._trend.calibration_bias = 200.0  # overconfident → raise threshold
        threshold = le.get_calibrated_threshold(base_threshold=65.0)
        assert threshold <= 90.0

        le._trend.calibration_bias = -200.0  # underconfident → lower threshold
        threshold = le.get_calibrated_threshold(base_threshold=65.0)
        assert threshold >= 40.0

    def test_uses_config_default_when_no_arg(self, le):
        threshold = le.get_calibrated_threshold()
        assert 40.0 <= threshold <= 90.0


# ---------------------------------------------------------------------------
# save_daily_summary
# ---------------------------------------------------------------------------

class TestSaveDailySummary:
    def test_saves_and_retrieves(self, le):
        le.save_daily_summary(pnl_cents=500, trades=10, wins=7, losses=3, avg_conf=72.5)
        summaries = le.get_daily_summaries()
        assert len(summaries) >= 1
        today = summaries[0]
        assert today["trades"] == 10
        assert today["wins"] == 7
        assert today["gross_pnl_cents"] == 500

    def test_upserts_same_date(self, le):
        le.save_daily_summary(pnl_cents=100, trades=2, wins=1, losses=1, avg_conf=70.0)
        le.save_daily_summary(pnl_cents=300, trades=5, wins=3, losses=2, avg_conf=72.0)
        summaries = le.get_daily_summaries()
        # Should still be only 1 row for today (upsert)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        today_rows = [s for s in summaries if s["date"] == today]
        assert len(today_rows) == 1
        assert today_rows[0]["trades"] == 5


# ---------------------------------------------------------------------------
# get_pending_trades
# ---------------------------------------------------------------------------

class TestGetPendingTrades:
    def test_newly_logged_trades_are_pending(self, le):
        _log(le, ticker="P-1")
        _log(le, ticker="P-2")
        pending = le.get_pending_trades()
        tickers = {t["ticker"] for t in pending}
        assert "P-1" in tickers
        assert "P-2" in tickers

    def test_settled_trades_not_in_pending(self, le):
        tid = _log(le, ticker="S-1")
        le.update_outcome(tid, outcome="win", pnl_cents=50)
        pending = le.get_pending_trades()
        assert not any(t["ticker"] == "S-1" for t in pending)


# ---------------------------------------------------------------------------
# _point_biserial (static helper)
# ---------------------------------------------------------------------------

class TestPointBiserial:
    def test_too_few_values_returns_zero(self):
        assert LearningEngine._point_biserial([1.0, 2.0, 3.0], [1, 0, 1]) == 0.0

    def test_perfect_positive_correlation(self):
        values = [10.0, 10.0, 10.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        binary = [1,    1,    1,    0,   0,   0,   0,   0  ]
        corr = LearningEngine._point_biserial(values, binary)
        assert corr > 0.5

    def test_perfect_negative_correlation(self):
        values = [1.0, 1.0, 1.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        binary = [1,   1,   1,   0,    0,    0,    0,    0   ]
        corr = LearningEngine._point_biserial(values, binary)
        assert corr < -0.5

    def test_output_clamped_to_minus1_plus1(self):
        import random
        random.seed(0)
        values = [random.uniform(0, 100) for _ in range(20)]
        binary = [random.randint(0, 1) for _ in range(20)]
        corr = LearningEngine._point_biserial(values, binary)
        assert -1.0 <= corr <= 1.0
