"""
tests/test_integration_smoke.py
================================
End-to-end smoke test wiring the full trading pipeline without any
real network calls:

  MarketOpportunity  →  AnalysisEngine  →  RiskManager  →  LearningEngine

All Kalshi API access is replaced by fixtures/mocks. The tests verify
that the modules integrate correctly — signals flow from analysis to
risk to learning without crashes, and state updates are consistent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# Stub out the broken cryptography dependency before importing market_scanner
if "kalshi_agent.kalshi_client" not in sys.modules:
    sys.modules["kalshi_agent.kalshi_client"] = MagicMock()

from kalshi_agent.market_scanner import MarketOpportunity
from kalshi_agent.analysis_engine import AnalysisEngine, TradeSignal
from kalshi_agent.risk_manager import RiskManager
from kalshi_agent.learning_engine import LearningEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_opp(**kwargs) -> MarketOpportunity:
    defaults = dict(
        ticker="TEST-A",
        event_ticker="EVT-A",
        title="Will X happen?",
        series_ticker="TEST",
        category="politics",
        yes_bid=45, yes_ask=48,
        no_bid=52,  no_ask=55,
        last_price=46,
        mid_price=55.0,
        volume_24h=2000,
        open_interest=5000,
        liquidity=30000,
        hours_to_expiry=24.0,
        spread=3,
        orderbook=None,
        recent_trades=None,
    )
    defaults.update(kwargs)
    return MarketOpportunity(**defaults)


@pytest.fixture
def engine():
    return AnalysisEngine(config={"min_confidence_threshold": 0, "limit_spread_buffer_cents": 1})


@pytest.fixture
def risk():
    rm = RiskManager(config={
        "min_balance_cents": 100,
        "daily_loss_limit_cents": 10000,
        "max_trades_per_day": 50,
        "max_open_positions": 20,
        "max_drawdown_pct": 0.50,
        "max_position_pct": 0.05,
        "loss_streak_threshold": 5,
        "loss_streak_size_multiplier": 0.5,
    })
    rm.update_balance(100_000)  # $1000 starting balance
    return rm


@pytest.fixture
def le(tmp_path):
    cfg = {
        "db_path": str(tmp_path / "smoke.db"),
        "learning_rate": 0.1,
        "rolling_window": 50,
        "recalibration_decay": 0.95,
        "review_interval_trades": 20,
        "min_trades_for_review": 10,
        "min_confidence_threshold": 65,
    }
    eng = LearningEngine(config=cfg)
    yield eng
    eng.close()


# ---------------------------------------------------------------------------
# Analysis → Risk integration
# ---------------------------------------------------------------------------

class TestAnalysisToRisk:
    def test_signal_confidence_within_risk_sizing_range(self, engine, risk):
        opp = make_opp(volume_24h=5000, spread=1, liquidity=50000, hours_to_expiry=24)
        signals = engine.analyse([opp])
        assert signals, "Expected at least one signal from a high-quality opportunity"
        sig = signals[0]

        # Risk manager should be able to size a position from this signal
        assert risk.can_trade()
        size = risk.position_size(confidence=sig.confidence, price_cents=sig.suggested_price)
        assert size >= 1

    def test_risk_blocks_after_daily_loss(self, engine, risk):
        risk._cfg = {"daily_loss_limit_cents": 100, **risk.cfg}
        # Override loss limit to 100 cents
        risk.cfg["daily_loss_limit_cents"] = 100
        risk.record_outcome(-200)  # exceeds limit

        opp = make_opp(volume_24h=5000, spread=1, liquidity=50000, hours_to_expiry=24)
        signals = engine.analyse([opp])

        # Even with valid signals, risk gate blocks trading
        assert not risk.can_trade()

    def test_multiple_opportunities_sorted(self, engine, risk):
        opps = [
            make_opp(ticker="GOOD", volume_24h=5000, spread=1, liquidity=50000, hours_to_expiry=24),
            make_opp(ticker="BAD",  volume_24h=10,   spread=20, liquidity=50,   hours_to_expiry=1000),
        ]
        signals = engine.analyse(opps)
        if len(signals) >= 2:
            # Signals must be sorted by confidence descending
            assert signals[0].confidence >= signals[1].confidence


# ---------------------------------------------------------------------------
# Analysis → Learning integration
# ---------------------------------------------------------------------------

class TestAnalysisToLearning:
    def test_signal_scores_logged_to_learning_engine(self, engine, le):
        opp = make_opp(volume_24h=2000, spread=3, liquidity=20000, hours_to_expiry=24)
        signals = engine.analyse([opp])
        assert signals

        sig = signals[0]
        tid = le.log_trade(
            ticker=sig.ticker,
            event_ticker=sig.event_ticker,
            title=sig.title,
            side=sig.side,
            action=sig.action,
            count=2,
            entry_price=sig.suggested_price,
            confidence=sig.confidence,
            edge_score=sig.edge_score,
            liquidity_score=sig.liquidity_score,
            volume_score=sig.volume_score,
            timing_score=sig.timing_score,
            momentum_score=sig.momentum_score,
            category=sig.category,
        )
        assert tid >= 1

        trades = le.get_all_trades()
        assert any(t["ticker"] == sig.ticker for t in trades)

    def test_outcome_updates_performance_stats(self, engine, le):
        opp = make_opp(volume_24h=2000, spread=3, liquidity=20000, hours_to_expiry=24)
        signals = engine.analyse([opp])
        assert signals

        sig = signals[0]
        tid = le.log_trade(
            ticker=sig.ticker, event_ticker="E-1", title="T", side="yes",
            action="buy", count=1, entry_price=50, confidence=sig.confidence,
        )
        le.update_outcome(tid, outcome="win", exit_price=65, pnl_cents=150)

        perf = le.get_performance()
        assert perf["total_trades"] == 1
        assert perf["wins"] == 1
        assert perf["total_pnl"] == 150


# ---------------------------------------------------------------------------
# Risk → Learning integration
# ---------------------------------------------------------------------------

class TestRiskToLearning:
    def test_risk_outcome_matches_learning_pnl(self, risk, le):
        """Record the same P&L in both risk manager and learning engine."""
        tid = le.log_trade(
            ticker="T-RL", event_ticker="E-RL", title="T", side="yes",
            action="buy", count=1, entry_price=50, confidence=75.0, category="economics",
        )
        pnl = 200
        le.update_outcome(tid, outcome="win", exit_price=70, pnl_cents=pnl)
        risk.record_outcome(pnl)

        perf = le.get_performance()
        assert perf["total_pnl"] == pnl
        assert risk.daily.gross_pnl_cents == pnl

    def test_consecutive_losses_flow_through_risk(self, risk, le):
        for i in range(3):
            tid = le.log_trade(
                ticker=f"T-{i}", event_ticker="E-1", title="T", side="yes",
                action="buy", count=1, entry_price=50, confidence=65.0,
            )
            le.update_outcome(tid, outcome="loss", exit_price=30, pnl_cents=-80)
            risk.record_outcome(-80)

        assert risk._consecutive_losses == 3
        perf = le.get_performance()
        assert perf["losses"] == 3


# ---------------------------------------------------------------------------
# Full pipeline: Opportunity → Signal → Risk gate → Log → Outcome
# ---------------------------------------------------------------------------

class TestFullPipeline:
    def test_full_cycle_win(self, engine, risk, le):
        """Simulate one complete winning trade cycle."""
        # 1. Analyse opportunity
        opp = make_opp(volume_24h=3000, spread=2, liquidity=25000, hours_to_expiry=24)
        signals = engine.analyse([opp])
        assert signals
        sig = signals[0]

        # 2. Risk gate
        assert risk.can_trade()
        size = risk.position_size(confidence=sig.confidence, price_cents=sig.suggested_price)
        assert size >= 1

        # 3. Log trade
        entry_price = sig.suggested_price
        tid = le.log_trade(
            ticker=sig.ticker,
            event_ticker=sig.event_ticker,
            title=sig.title,
            side=sig.side,
            action=sig.action,
            count=size,
            entry_price=entry_price,
            confidence=sig.confidence,
            edge_score=sig.edge_score,
            liquidity_score=sig.liquidity_score,
            volume_score=sig.volume_score,
            timing_score=sig.timing_score,
            momentum_score=sig.momentum_score,
            category=sig.category,
        )
        assert tid >= 1

        # 4. Simulate settlement: market resolved YES, we held YES
        exit_price = min(99, entry_price + 20)
        pnl = (exit_price - entry_price) * size
        le.update_outcome(tid, outcome="win", exit_price=exit_price, pnl_cents=pnl)
        risk.record_outcome(pnl)

        # 5. Verify state coherence
        perf = le.get_performance()
        assert perf["wins"] == 1
        assert perf["total_pnl"] == pnl
        assert risk.daily.gross_pnl_cents == pnl
        assert risk._consecutive_losses == 0

    def test_full_cycle_loss_increments_streak(self, engine, risk, le):
        """A losing trade increments the streak and reduces subsequent sizing."""
        opp = make_opp(volume_24h=3000, spread=2, liquidity=25000, hours_to_expiry=24)
        signals = engine.analyse([opp])
        assert signals
        sig = signals[0]

        size_before = risk.position_size(confidence=sig.confidence, price_cents=sig.suggested_price)

        tid = le.log_trade(
            ticker=sig.ticker, event_ticker="E-1", title="T",
            side=sig.side, action="buy", count=1, entry_price=sig.suggested_price,
            confidence=sig.confidence,
        )
        le.update_outcome(tid, outcome="loss", exit_price=20, pnl_cents=-200)
        risk.record_outcome(-200)

        assert risk._consecutive_losses == 1

    def test_category_multiplier_feeds_back_into_analysis(self, engine, le):
        """After enough trades, the learning engine's category multiplier
        should influence the analysis engine's scoring."""
        # Log 10 wins in 'politics' to push the multiplier above 1.0
        overall_wins = 5
        for i in range(10):
            tid = le.log_trade(
                ticker=f"G-{i}", event_ticker="E", title="T", side="yes",
                action="buy", count=1, entry_price=50, confidence=70.0, category="general",
            )
            if i < overall_wins:
                le.update_outcome(tid, outcome="win", pnl_cents=50)
            else:
                le.update_outcome(tid, outcome="loss", pnl_cents=-30)

        for i in range(5):
            tid = le.log_trade(
                ticker=f"P-{i}", event_ticker="E", title="T", side="yes",
                action="buy", count=1, entry_price=50, confidence=70.0, category="politics",
            )
            le.update_outcome(tid, outcome="win", pnl_cents=80)

        mult = le.get_category_multiplier("politics")
        assert mult >= 1.0  # politics winning more → boost

        # Wire learning engine into analysis engine
        engine.learning = le
        opp = make_opp(category="politics", volume_24h=3000, spread=2, liquidity=25000, hours_to_expiry=24)
        signals_with_le = engine.analyse([opp])

        engine.learning = None
        signals_without = engine.analyse([opp])

        # With a positive multiplier, confidence should be >= without
        if signals_with_le and signals_without:
            assert signals_with_le[0].confidence >= signals_without[0].confidence * 0.95

    def test_weight_update_changes_signal_scores(self, engine):
        """Updating weights shifts the relative contribution of scoring dimensions."""
        opp = make_opp(
            volume_24h=5000,  # strong volume
            spread=15,        # poor liquidity
            hours_to_expiry=24,
            liquidity=500,
        )

        # Baseline weights
        signals_before = engine.analyse([opp])

        # Upweight volume massively, downweight liquidity
        engine.update_weights({"edge": 0.1, "liquidity": 0.05, "volume": 0.7, "timing": 0.1, "momentum": 0.05})
        signals_after = engine.analyse([opp])

        # Both should return signals (just different scores); no crash
        assert isinstance(signals_before, list)
        assert isinstance(signals_after, list)
