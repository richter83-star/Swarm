"""
tests/test_meta_learning.py
============================

Tests for the cross-bot meta-learning system:
  - CrossBotInsights (dataclass + serialisation)
  - SwarmMetaAggregator (DB reads + aggregation logic)
  - MetaLearner.get_swarm_category_multiplier (blending logic)
  - _point_biserial (module-level helper)
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarm.meta_learning import (
    CrossBotInsights,
    CategoryEdge,
    MetaLearner,
    SwarmMetaAggregator,
    _point_biserial,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_trade_db(path: Path, trades: list[dict]) -> None:
    """Create a minimal trades SQLite DB with the given rows."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT    NOT NULL DEFAULT '',
            ticker          TEXT    NOT NULL DEFAULT '',
            event_ticker    TEXT,
            title           TEXT,
            series_ticker   TEXT,
            category        TEXT,
            bot_name        TEXT,
            side            TEXT    NOT NULL DEFAULT 'yes',
            action          TEXT    NOT NULL DEFAULT 'buy',
            count           INTEGER NOT NULL DEFAULT 1,
            entry_price     INTEGER NOT NULL DEFAULT 50,
            fill_price      INTEGER,
            slippage_cents  INTEGER,
            fill_status     TEXT    DEFAULT 'unknown',
            order_id        TEXT,
            confidence      REAL    NOT NULL DEFAULT 70.0,
            edge_score      REAL    DEFAULT 0.0,
            liquidity_score REAL    DEFAULT 0.0,
            volume_score    REAL    DEFAULT 0.0,
            timing_score    REAL    DEFAULT 0.0,
            momentum_score  REAL    DEFAULT 0.0,
            rationale       TEXT,
            outcome         TEXT,
            exit_price      INTEGER,
            pnl_cents       INTEGER,
            settled_at      TEXT,
            pnl_valid       INTEGER DEFAULT 1,
            pnl_validation_reason TEXT,
            reconciliation_trace  TEXT
        )
    """)
    for t in trades:
        conn.execute(
            """INSERT INTO trades
               (timestamp, ticker, category, bot_name, side, action,
                count, entry_price, confidence, outcome, pnl_cents, settled_at,
                edge_score, liquidity_score, volume_score, timing_score, momentum_score,
                pnl_valid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.get("timestamp", "2026-01-01T00:00:00+00:00"),
                t.get("ticker", "TEST-A"),
                t.get("category", "economics"),
                t.get("bot_name", "oracle"),
                t.get("side", "yes"),
                t.get("action", "buy"),
                t.get("count", 1),
                t.get("entry_price", 50),
                t.get("confidence", 70.0),
                t.get("outcome", "win"),
                t.get("pnl_cents", 10),
                t.get("settled_at", "2026-01-01T01:00:00+00:00"),
                t.get("edge_score", 60.0),
                t.get("liquidity_score", 70.0),
                t.get("volume_score", 55.0),
                t.get("timing_score", 65.0),
                t.get("momentum_score", 50.0),
                t.get("pnl_valid", 1),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def oracle_db(tmp_path):
    db = tmp_path / "oracle.db"
    trades = (
        # 14 wins, 6 losses in economics → 70% win rate
        [{"category": "economics", "bot_name": "oracle", "outcome": "win", "pnl_cents": 15,
          "edge_score": 70.0, "liquidity_score": 65.0}] * 14
        + [{"category": "economics", "bot_name": "oracle", "outcome": "loss", "pnl_cents": -20,
            "edge_score": 30.0, "liquidity_score": 40.0}] * 6
    )
    _make_trade_db(db, trades)
    return db


@pytest.fixture()
def sentinel_db(tmp_path):
    db = tmp_path / "sentinel.db"
    trades = (
        # 8 wins, 12 losses in politics → 40% win rate (cold)
        [{"category": "politics", "bot_name": "sentinel", "outcome": "win", "pnl_cents": 12,
          "edge_score": 55.0}] * 8
        + [{"category": "politics", "bot_name": "sentinel", "outcome": "loss", "pnl_cents": -18,
            "edge_score": 35.0}] * 12
    )
    _make_trade_db(db, trades)
    return db


@pytest.fixture()
def aggregator(tmp_path):
    return SwarmMetaAggregator(
        project_root=str(tmp_path),
        config={
            "aggregation_interval_seconds": 1800,
            "min_trades_per_category": 5,
            "insights_max_age_seconds": 7200,
        },
    )


# ---------------------------------------------------------------------------
# _point_biserial
# ---------------------------------------------------------------------------

def test_point_biserial_perfect_separation():
    # All wins have value 1.0, all losses have 0.0 → strong positive correlation
    values = [1.0] * 10 + [0.0] * 10
    binary = [1.0] * 10 + [0.0] * 10
    r = _point_biserial(values, binary)
    assert r > 0.8


def test_point_biserial_no_separation():
    # Values identical → zero correlation
    values = [0.5] * 20
    binary = [1.0] * 10 + [0.0] * 10
    r = _point_biserial(values, binary)
    assert r == 0.0


def test_point_biserial_insufficient_data():
    assert _point_biserial([1.0, 2.0], [1.0, 0.0]) == 0.0


def test_point_biserial_all_wins():
    # No variance in binary → zero
    values = [50.0, 60.0, 70.0]
    binary = [1.0, 1.0, 1.0]
    assert _point_biserial(values, binary) == 0.0


# ---------------------------------------------------------------------------
# CategoryEdge + CrossBotInsights
# ---------------------------------------------------------------------------

def _make_insights(
    econ_win_rate=60.0,
    econ_trades=30,
    swarm_win_rate=55.0,
    swarm_total=60,
    min_thresh=10,
) -> CrossBotInsights:
    return CrossBotInsights(
        generated_at="2026-01-01T00:00:00+00:00",
        swarm_win_rate=swarm_win_rate,
        swarm_avg_pnl_cents=10.0,
        swarm_total_trades=swarm_total,
        hot_categories=["economics"] if econ_win_rate > swarm_win_rate + 5 else [],
        cold_categories=[],
        categories={
            "economics": CategoryEdge(
                win_rate=econ_win_rate,
                avg_pnl_cents=12.0,
                total_trades=econ_trades,
                best_bot="oracle",
                feature_weights={"edge": 0.35, "liquidity": 0.25, "volume": 0.20,
                                  "timing": 0.12, "momentum": 0.08},
            )
        },
        bot_summaries={"oracle": {"win_rate": 60.0, "avg_pnl_cents": 12.0, "total_trades": 30}},
        min_trades_threshold=min_thresh,
    )


def test_get_category_multiplier_hot_category():
    insights = _make_insights(econ_win_rate=65.0, swarm_win_rate=50.0)
    mult = insights.get_category_multiplier("economics")
    assert mult is not None
    assert mult > 1.0


def test_get_category_multiplier_cold_category():
    insights = _make_insights(econ_win_rate=40.0, swarm_win_rate=55.0)
    mult = insights.get_category_multiplier("economics")
    assert mult is not None
    assert mult < 1.0


def test_get_category_multiplier_capped_at_1_3():
    insights = _make_insights(econ_win_rate=99.0, swarm_win_rate=10.0)
    mult = insights.get_category_multiplier("economics")
    assert mult <= 1.3


def test_get_category_multiplier_capped_at_0_7():
    insights = _make_insights(econ_win_rate=5.0, swarm_win_rate=90.0)
    mult = insights.get_category_multiplier("economics")
    assert mult >= 0.7


def test_get_category_multiplier_unknown_category():
    insights = _make_insights()
    assert insights.get_category_multiplier("sports") is None


def test_get_category_multiplier_insufficient_swarm_trades():
    insights = _make_insights(swarm_total=10)  # < 20
    assert insights.get_category_multiplier("economics") is None


def test_get_category_multiplier_insufficient_category_trades():
    insights = _make_insights(econ_trades=3, min_thresh=5)  # 3 < 5
    assert insights.get_category_multiplier("economics") is None


def test_get_feature_weights_returns_dict():
    insights = _make_insights()
    fw = insights.get_feature_weights("economics")
    assert isinstance(fw, dict)
    assert "edge" in fw


def test_get_feature_weights_unknown_category():
    insights = _make_insights()
    assert insights.get_feature_weights("crypto") is None


# ---------------------------------------------------------------------------
# CrossBotInsights serialisation round-trip
# ---------------------------------------------------------------------------

def test_insights_round_trip():
    insights = _make_insights()
    d = insights.to_dict()
    restored = CrossBotInsights.from_dict(d)
    assert restored.swarm_win_rate == insights.swarm_win_rate
    assert restored.swarm_total_trades == insights.swarm_total_trades
    assert "economics" in restored.categories
    assert restored.categories["economics"].best_bot == "oracle"


def test_insights_from_dict_handles_missing_fields():
    # Should not raise even with minimal dict
    restored = CrossBotInsights.from_dict({
        "generated_at": "2026-01-01T00:00:00+00:00",
        "swarm_win_rate": 50.0,
        "swarm_total_trades": 100,
    })
    assert restored.categories == {}
    assert restored.hot_categories == []


# ---------------------------------------------------------------------------
# SwarmMetaAggregator
# ---------------------------------------------------------------------------

def test_aggregator_should_aggregate_initially(aggregator):
    assert aggregator.should_aggregate() is True


def test_aggregator_should_not_aggregate_after_run(aggregator, tmp_path, oracle_db):
    bot_db_paths = {"oracle": oracle_db}
    aggregator.aggregate(bot_db_paths)
    assert aggregator.should_aggregate() is False


def test_aggregator_produces_insights(aggregator, tmp_path, oracle_db, sentinel_db):
    bot_db_paths = {"oracle": oracle_db, "sentinel": sentinel_db}
    insights = aggregator.aggregate(bot_db_paths)
    assert insights is not None
    assert insights.swarm_total_trades == 40  # 20 oracle + 20 sentinel
    assert "economics" in insights.categories
    assert "politics" in insights.categories


def test_aggregator_economics_hot(aggregator, tmp_path, oracle_db):
    """Oracle has 70% win rate in economics; with swarm avg ~70% it's at-par but should exist."""
    insights = aggregator.aggregate({"oracle": oracle_db})
    assert insights is not None
    assert insights.categories["economics"].win_rate == pytest.approx(70.0)
    assert insights.categories["economics"].best_bot == "oracle"


def test_aggregator_politics_cold(aggregator, tmp_path, oracle_db, sentinel_db):
    """When oracle dominates economics at 70% and sentinel has politics at 40%,
    politics should be cold relative to the swarm average."""
    insights = aggregator.aggregate({"oracle": oracle_db, "sentinel": sentinel_db})
    assert insights is not None
    assert insights.categories["politics"].win_rate == pytest.approx(40.0)
    # Swarm avg is ~55% (20 wins across 40 trades); politics at 40% is 15pp below → cold
    assert "politics" in insights.cold_categories


def test_aggregator_writes_json_file(aggregator, tmp_path, oracle_db):
    aggregator.aggregate({"oracle": oracle_db})
    insights_path = tmp_path / "data" / "swarm_meta_insights.json"
    assert insights_path.exists()
    with insights_path.open() as fh:
        data = json.load(fh)
    assert "generated_at" in data
    assert "categories" in data


def test_aggregator_load_insights_round_trip(aggregator, tmp_path, oracle_db):
    aggregator.aggregate({"oracle": oracle_db})
    loaded = SwarmMetaAggregator.load_insights(project_root=str(tmp_path))
    assert loaded is not None
    assert "economics" in loaded.categories


def test_aggregator_load_insights_missing_file(tmp_path):
    result = SwarmMetaAggregator.load_insights(project_root=str(tmp_path))
    assert result is None


def test_aggregator_load_insights_stale_file(tmp_path):
    """Insights with an old timestamp should be rejected."""
    # Write a valid but old-timestamped insights file directly
    path = tmp_path / "data" / "swarm_meta_insights.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_data = {
        "generated_at": "2000-01-01T00:00:00+00:00",
        "swarm_win_rate": 50.0,
        "swarm_avg_pnl_cents": 0.0,
        "swarm_total_trades": 100,
        "hot_categories": [],
        "cold_categories": [],
        "min_trades_threshold": 5,
        "categories": {},
        "bot_summaries": {},
    }
    with path.open("w") as fh:
        json.dump(stale_data, fh)
    # Any reasonable max_age should reject a year-2000 timestamp
    result = SwarmMetaAggregator.load_insights(
        project_root=str(tmp_path),
        max_age_seconds=3600,
    )
    assert result is None


def test_aggregator_skips_missing_db(aggregator, tmp_path):
    """Non-existent DB paths should be silently skipped."""
    missing = tmp_path / "missing.db"
    insights = aggregator.aggregate({"ghost_bot": missing})
    assert insights is not None
    assert insights.swarm_total_trades == 0


def test_aggregator_feature_weights_sum_to_one(aggregator, tmp_path, oracle_db):
    insights = aggregator.aggregate({"oracle": oracle_db})
    fw = insights.get_feature_weights("economics")
    assert fw is not None
    total = sum(fw.values())
    assert abs(total - 1.0) < 0.01


# ---------------------------------------------------------------------------
# MetaLearner.get_swarm_category_multiplier
# ---------------------------------------------------------------------------

def test_swarm_category_multiplier_no_swarm_data_returns_own(tmp_path):
    ml = MetaLearner(project_root=str(tmp_path), bot_name="test")
    insights = _make_insights(econ_trades=3, min_thresh=5)  # insufficient data
    result = ml.get_swarm_category_multiplier(
        category="economics",
        own_multiplier=1.1,
        insights=insights,
        own_trades=20,
    )
    assert result == pytest.approx(1.1)


def test_swarm_category_multiplier_hot_category_boosts(tmp_path):
    ml = MetaLearner(project_root=str(tmp_path), bot_name="test")
    insights = _make_insights(econ_win_rate=70.0, swarm_win_rate=50.0)
    result = ml.get_swarm_category_multiplier(
        category="economics",
        own_multiplier=1.0,
        insights=insights,
        own_trades=5,  # sparse own data → swarm gets higher weight
    )
    assert result > 1.0


def test_swarm_category_multiplier_cold_category_reduces(tmp_path):
    ml = MetaLearner(project_root=str(tmp_path), bot_name="test")
    insights = _make_insights(econ_win_rate=35.0, swarm_win_rate=60.0)
    result = ml.get_swarm_category_multiplier(
        category="economics",
        own_multiplier=1.0,
        insights=insights,
        own_trades=5,
    )
    assert result < 1.0


def test_swarm_category_multiplier_bounded(tmp_path):
    ml = MetaLearner(project_root=str(tmp_path), bot_name="test")
    # Extreme signals
    insights = _make_insights(econ_win_rate=99.0, swarm_win_rate=5.0)
    result = ml.get_swarm_category_multiplier(
        category="economics",
        own_multiplier=1.3,
        insights=insights,
        own_trades=50,
    )
    assert result <= 1.3


def test_swarm_category_multiplier_own_data_dominates_when_ample(tmp_path):
    ml = MetaLearner(project_root=str(tmp_path), bot_name="test")
    insights = _make_insights(econ_win_rate=70.0, swarm_win_rate=50.0)
    swarm_only_mult = insights.get_category_multiplier("economics")

    # With 80 own trades the bot trusts itself more (w_own = 0.75)
    result = ml.get_swarm_category_multiplier(
        category="economics",
        own_multiplier=0.9,   # own says this category is slightly cold
        insights=insights,
        own_trades=80,
    )
    # result should be between own (0.9) and swarm (>1.0), but closer to own
    assert 0.9 <= result <= swarm_only_mult
