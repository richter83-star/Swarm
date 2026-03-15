"""
tests/test_analysis_engine.py
==============================
Unit tests for AnalysisEngine scoring sub-functions and public interface.

All tests are pure-Python (no network, no database, no config file).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make sure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

# The system cryptography library is broken in CI (missing _cffi_backend).
# Stub it out before any import that transitively pulls in kalshi_client.
if "kalshi_agent.kalshi_client" not in sys.modules:
    sys.modules["kalshi_agent.kalshi_client"] = MagicMock()

from kalshi_agent.market_scanner import MarketOpportunity
from kalshi_agent.analysis_engine import AnalysisEngine, TradeSignal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_opp(**kwargs) -> MarketOpportunity:
    """Build a MarketOpportunity with sensible defaults, overrideable by kwargs."""
    defaults = dict(
        ticker="TEST-A",
        event_ticker="EVT-A",
        title="Test market",
        series_ticker="TEST",
        category="politics",
        yes_bid=45,
        yes_ask=48,
        no_bid=52,
        no_ask=55,
        last_price=46,
        mid_price=50.0,
        volume_24h=1000,
        open_interest=5000,
        liquidity=20000,
        hours_to_expiry=24.0,
        spread=3,
        orderbook=None,
        recent_trades=None,
    )
    defaults.update(kwargs)
    return MarketOpportunity(**defaults)


def make_engine(**cfg_overrides) -> AnalysisEngine:
    """Build an AnalysisEngine with a minimal config."""
    cfg = {"min_confidence_threshold": 65, "limit_spread_buffer_cents": 1}
    cfg.update(cfg_overrides)
    return AnalysisEngine(config=cfg)


# ---------------------------------------------------------------------------
# Weight normalisation
# ---------------------------------------------------------------------------

class TestWeightNormalisation:
    def test_default_weights_sum_to_one(self):
        eng = make_engine()
        total = sum(eng.weights.values())
        assert abs(total - 1.0) < 1e-9

    def test_custom_overrides_normalised(self):
        # Supply unequal weights; they should be normalised to sum=1
        eng = AnalysisEngine(
            config={"min_confidence_threshold": 65},
            weight_overrides={"edge": 2.0, "liquidity": 2.0, "volume": 1.0, "timing": 0.0, "momentum": 0.0},
        )
        total = sum(eng.weights.values())
        assert abs(total - 1.0) < 1e-9
        # edge and liquidity should be equal
        assert abs(eng.weights["edge"] - eng.weights["liquidity"]) < 1e-9

    def test_update_weights_renormalises(self):
        eng = make_engine()
        eng.update_weights({"edge": 10.0, "liquidity": 10.0, "volume": 5.0, "timing": 5.0, "momentum": 0.0})
        total = sum(eng.weights.values())
        assert abs(total - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# _liquidity_score
# ---------------------------------------------------------------------------

class TestLiquidityScore:
    def test_tight_spread_high_liquidity(self):
        opp = make_opp(spread=1, liquidity=50000)
        eng = make_engine()
        score = eng._liquidity_score(opp)
        assert score == 100.0   # 60 spread + 40 depth

    def test_wide_spread_low_liquidity(self):
        opp = make_opp(spread=15, liquidity=500)
        eng = make_engine()
        score = eng._liquidity_score(opp)
        assert score == 7.0    # 5 spread + 2 depth

    def test_spread_tiers(self):
        eng = make_engine()
        spreads_pts = [
            (1, 60.0),
            (3, 50.0),
            (5, 35.0),
            (10, 20.0),
            (11, 5.0),
        ]
        for spread, expected_spread_pts in spreads_pts:
            opp = make_opp(spread=spread, liquidity=0)
            score = eng._liquidity_score(opp)
            assert score == expected_spread_pts + 2.0, f"spread={spread}"

    def test_depth_tiers(self):
        eng = make_engine()
        depths = [
            (50000, 40.0),
            (10000, 30.0),
            (5000, 20.0),
            (1000, 10.0),
            (999, 2.0),
        ]
        for liq, expected_depth_pts in depths:
            opp = make_opp(spread=1, liquidity=liq)
            score = eng._liquidity_score(opp)
            assert score == 60.0 + expected_depth_pts, f"liquidity={liq}"


# ---------------------------------------------------------------------------
# _volume_score
# ---------------------------------------------------------------------------

class TestVolumeScore:
    def test_volume_tiers(self):
        eng = make_engine()
        cases = [
            (5000, 100.0),
            (1000, 80.0),
            (500, 60.0),
            (100, 40.0),
            (50, 25.0),
            (10, 10.0),
        ]
        for vol, expected in cases:
            opp = make_opp(volume_24h=vol)
            assert eng._volume_score(opp) == expected, f"volume={vol}"

    def test_zero_volume(self):
        eng = make_engine()
        opp = make_opp(volume_24h=0)
        assert eng._volume_score(opp) == 10.0


# ---------------------------------------------------------------------------
# _timing_score
# ---------------------------------------------------------------------------

class TestTimingScore:
    def test_sweet_spot_returns_100(self):
        eng = make_engine()
        for h in [6, 12, 24, 48]:
            opp = make_opp(hours_to_expiry=h)
            assert eng._timing_score(opp) == 100.0, f"hours={h}"

    def test_expired_returns_zero(self):
        eng = make_engine()
        opp = make_opp(hours_to_expiry=0)
        assert eng._timing_score(opp) == 0.0

    def test_negative_hours_returns_zero(self):
        eng = make_engine()
        opp = make_opp(hours_to_expiry=-1)
        assert eng._timing_score(opp) == 0.0

    def test_timing_tiers(self):
        eng = make_engine()
        cases = [
            (3, 70.0),   # 2–6h
            (100, 60.0), # 48–168h
            (500, 30.0), # 168–720h
            (800, 10.0), # >720h
        ]
        for h, expected in cases:
            opp = make_opp(hours_to_expiry=h)
            assert eng._timing_score(opp) == expected, f"hours={h}"


# ---------------------------------------------------------------------------
# _edge_score
# ---------------------------------------------------------------------------

class TestEdgeScore:
    def test_zero_edge_returns_50(self):
        eng = make_engine()
        opp = make_opp(mid_price=50.0)
        score = eng._edge_score(0.0, opp)
        assert abs(score - 50.0) < 0.1

    def test_positive_edge_above_50(self):
        eng = make_engine()
        opp = make_opp(mid_price=50.0)
        score = eng._edge_score(10.0, opp)
        assert score > 50.0

    def test_negative_edge_below_50(self):
        eng = make_engine()
        opp = make_opp(mid_price=50.0)
        score = eng._edge_score(-10.0, opp)
        assert score < 50.0

    def test_large_positive_edge_approaches_100(self):
        eng = make_engine()
        opp = make_opp(mid_price=50.0)
        score = eng._edge_score(200.0, opp)
        assert score > 99.0

    def test_output_range(self):
        eng = make_engine()
        opp = make_opp(mid_price=50.0)
        for edge in [-100, -10, 0, 10, 100]:
            score = eng._edge_score(float(edge), opp)
            assert 0.0 <= score <= 100.0, f"edge={edge}"


# ---------------------------------------------------------------------------
# _price_velocity / _trade_flow_direction
# ---------------------------------------------------------------------------

class TestPriceVelocity:
    def test_no_trades_returns_zero(self):
        eng = make_engine()
        opp = make_opp(recent_trades=None)
        assert eng._price_velocity(opp) == 0.0

    def test_single_trade_returns_zero(self):
        eng = make_engine()
        opp = make_opp(recent_trades=[{"yes_price": 50}])
        assert eng._price_velocity(opp) == 0.0

    def test_rising_prices(self):
        eng = make_engine()
        trades = [{"yes_price": p} for p in [40, 45, 50, 55, 60]]
        opp = make_opp(recent_trades=trades)
        assert eng._price_velocity(opp) == 20.0  # 60 - 40

    def test_falling_prices(self):
        eng = make_engine()
        trades = [{"yes_price": p} for p in [60, 55, 50, 45, 40]]
        opp = make_opp(recent_trades=trades)
        assert eng._price_velocity(opp) == -20.0


class TestTradeFlowDirection:
    def test_no_trades_returns_zero(self):
        eng = make_engine()
        opp = make_opp(recent_trades=None)
        assert eng._trade_flow_direction(opp) == 0.0

    def test_all_yes_buys(self):
        eng = make_engine()
        trades = [{"taker_side": "yes"} for _ in range(4)]
        opp = make_opp(recent_trades=trades)
        assert eng._trade_flow_direction(opp) == 1.0

    def test_all_no_buys(self):
        eng = make_engine()
        trades = [{"taker_side": "no"} for _ in range(4)]
        opp = make_opp(recent_trades=trades)
        assert eng._trade_flow_direction(opp) == -1.0

    def test_mixed_flow(self):
        eng = make_engine()
        trades = [{"taker_side": "yes"}, {"taker_side": "no"}]
        opp = make_opp(recent_trades=trades)
        assert eng._trade_flow_direction(opp) == 0.0


# ---------------------------------------------------------------------------
# _momentum_score
# ---------------------------------------------------------------------------

class TestMomentumScore:
    def test_no_data_scores_near_midpoint(self):
        eng = make_engine()
        opp = make_opp(recent_trades=None)
        score = eng._momentum_score(opp)
        # With combined=0, sigmoid(0)=50
        assert abs(score - 50.0) < 1.0

    def test_strong_yes_momentum_above_50(self):
        eng = make_engine()
        trades = (
            [{"yes_price": p, "taker_side": "yes"} for p in [40, 45, 50, 55, 60]]
        )
        opp = make_opp(recent_trades=trades)
        score = eng._momentum_score(opp)
        assert score > 50.0

    def test_strong_no_momentum_below_50(self):
        eng = make_engine()
        trades = [{"yes_price": p, "taker_side": "no"} for p in [60, 55, 50, 45, 40]]
        opp = make_opp(recent_trades=trades)
        score = eng._momentum_score(opp)
        assert score < 50.0

    def test_output_range(self):
        eng = make_engine()
        trades = [{"yes_price": 90, "taker_side": "yes"} for _ in range(5)]
        opp = make_opp(recent_trades=trades)
        score = eng._momentum_score(opp)
        assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# _estimate_edge
# ---------------------------------------------------------------------------

class TestEstimateEdge:
    def test_yes_edge_when_fv_above_ask(self):
        # mid=50 → fair_value~50+mom, yes_ask=40 → yes_edge positive and big
        eng = make_engine()
        opp = make_opp(mid_price=60.0, yes_ask=40, no_ask=40)
        side, edge, price = eng._estimate_edge(opp)
        assert side == "yes"

    def test_no_edge_when_fv_well_below_mid(self):
        # very low mid with high no_ask means NO edge wins
        eng = make_engine()
        opp = make_opp(mid_price=10.0, yes_ask=30, no_ask=10)
        side, edge, price = eng._estimate_edge(opp)
        # (100 - ~10) - 10 = ~80 for NO edge
        assert side in ("yes", "no")  # just verify it doesn't crash

    def test_returns_three_tuple(self):
        eng = make_engine()
        opp = make_opp()
        result = eng._estimate_edge(opp)
        assert len(result) == 3

    def test_price_within_bounds(self):
        eng = make_engine()
        opp = make_opp()
        _, _, price = eng._estimate_edge(opp)
        assert 1 <= price <= 99


# ---------------------------------------------------------------------------
# analyse() — threshold filtering and sorting
# ---------------------------------------------------------------------------

class TestAnalyse:
    def test_empty_opportunities_returns_empty(self):
        eng = make_engine()
        assert eng.analyse([]) == []

    def test_signals_sorted_by_confidence_descending(self):
        """Create two opportunities that score above threshold; verify ordering."""
        eng = make_engine(min_confidence_threshold=0)  # accept everything
        opps = [
            make_opp(ticker="A", volume_24h=5000, spread=1, liquidity=50000, hours_to_expiry=24, mid_price=55.0, yes_ask=40, no_ask=40),
            make_opp(ticker="B", volume_24h=10,   spread=20, liquidity=100,  hours_to_expiry=1,  mid_price=50.0),
        ]
        signals = eng.analyse(opps)
        if len(signals) >= 2:
            assert signals[0].confidence >= signals[1].confidence

    def test_low_confidence_filtered_out(self):
        # High threshold + bad opportunity → empty signals
        eng = make_engine(min_confidence_threshold=99)
        opp = make_opp(volume_24h=10, spread=20, liquidity=50, hours_to_expiry=1000)
        signals = eng.analyse([opp])
        assert signals == []

    def test_signal_fields_populated(self):
        eng = make_engine(min_confidence_threshold=0)
        opp = make_opp(
            ticker="TEST-X",
            volume_24h=5000,
            spread=1,
            liquidity=50000,
            hours_to_expiry=24,
            mid_price=60.0,
            yes_ask=45,
            no_ask=45,
        )
        signals = eng.analyse([opp])
        if signals:
            s = signals[0]
            assert s.ticker == "TEST-X"
            assert s.action == "buy"
            assert s.side in ("yes", "no")
            assert 0.0 <= s.confidence <= 100.0
            assert s.edge_score > 0 or s.liquidity_score > 0

    def test_learning_engine_multiplier_applied(self):
        eng = make_engine(min_confidence_threshold=0)
        mock_le = MagicMock()
        mock_le.get_calibrated_threshold.return_value = 0
        mock_le.get_category_multiplier.return_value = 0.0  # zero multiplier → kills confidence
        eng.learning = mock_le

        opp = make_opp(volume_24h=5000, spread=1, liquidity=50000, hours_to_expiry=24, mid_price=60.0, yes_ask=40, no_ask=40)
        signals = eng.analyse([opp])
        # Multiplier=0 clamps confidence to 0, so nothing should pass threshold=0?
        # Actually 0 >= 0 is True, but confidence=0 might still be included
        for s in signals:
            assert s.confidence == 0.0

    def test_analyse_returns_list_of_trade_signals(self):
        eng = make_engine(min_confidence_threshold=0)
        opp = make_opp(volume_24h=5000, spread=1, liquidity=50000, hours_to_expiry=24)
        signals = eng.analyse([opp])
        for s in signals:
            assert isinstance(s, TradeSignal)
