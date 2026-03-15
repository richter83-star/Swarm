"""
learning_engine.py
==================

Enhanced trade logger and strategy recalibrator with:

* **Trend detection** — rolling momentum in win rate, P&L, and per-category
  performance so the agent doubles down on hot categories and backs off cold ones.
* **Confidence calibration curves** — isotonic-regression-style mapping from
  raw confidence scores to observed win probabilities, so the scoring engine
  knows whether it's over- or under-confident in each band.
* **Feature importance scoring** — tracks which sub-scores (edge, liquidity,
  volume, timing, momentum) are genuinely predictive of wins using point-biserial
  correlation, feeding better weight updates.
* **Per-category win tracking** — separate win/loss tallies per market
  category so the agent can tilt toward categories where it has edge.
* **Decay-weighted recalibration** — recent trades are weighted more heavily
  than old ones during weight updates, preventing stale data from dominating.

All data persists in SQLite for durability across restarts.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    event_ticker    TEXT,
    title           TEXT,
    series_ticker   TEXT,
    category        TEXT,
    bot_name        TEXT,
    side            TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    count           INTEGER NOT NULL,
    entry_price     INTEGER NOT NULL,
    fill_price      INTEGER,
    slippage_cents  INTEGER,
    fill_status     TEXT    DEFAULT 'unknown',
    order_id        TEXT,
    confidence      REAL    NOT NULL,
    edge_score      REAL,
    liquidity_score REAL,
    volume_score    REAL,
    timing_score    REAL,
    momentum_score  REAL,
    rationale       TEXT,
    outcome         TEXT,
    exit_price      INTEGER,
    pnl_cents       INTEGER,
    settled_at      TEXT,
    pnl_valid       INTEGER DEFAULT 1,
    pnl_validation_reason TEXT,
    reconciliation_trace TEXT
);

CREATE TABLE IF NOT EXISTS weight_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    edge            REAL    NOT NULL,
    liquidity       REAL    NOT NULL,
    volume          REAL    NOT NULL,
    timing          REAL    NOT NULL,
    momentum        REAL    NOT NULL DEFAULT 0,
    win_rate        REAL,
    avg_pnl         REAL,
    trade_count     INTEGER,
    trigger_reason  TEXT
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date            TEXT PRIMARY KEY,
    trades          INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    gross_pnl_cents INTEGER NOT NULL DEFAULT 0,
    avg_confidence  REAL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS category_stats (
    category        TEXT PRIMARY KEY,
    trades          INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    total_pnl_cents INTEGER NOT NULL DEFAULT 0,
    last_updated    TEXT
);

CREATE TABLE IF NOT EXISTS calibration_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp         TEXT NOT NULL,
    bucket            INTEGER NOT NULL,
    trades            INTEGER NOT NULL,
    wins              INTEGER NOT NULL,
    observed_win_rate REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker    ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_outcome   ON trades(outcome);
CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_category  ON trades(category);
CREATE INDEX IF NOT EXISTS idx_trades_series    ON trades(series_ticker);
"""


class TrendSnapshot:
    """Lightweight container for the agent's current momentum state."""
    def __init__(self):
        self.win_rate_trend: float = 0.0
        self.pnl_trend: float = 0.0
        self.hot_categories: List[str] = []
        self.cold_categories: List[str] = []
        self.calibration_bias: float = 0.0
        self.feature_importance: Dict[str, float] = {}
        self.momentum_multiplier: float = 1.0


class LearningEngine:
    """
    Persistent trade logger and strategy recalibrator.

    Parameters
    ----------
    config : dict
        The ``learning`` section of ``config.yaml``.
    """

    def __init__(self, config: Dict[str, Any]):
        self.cfg = config
        db_path = Path(config.get("db_path", "data/kalshi_agent.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode: concurrent reads from dashboard don't block bot writes
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Wait up to 30 s instead of instantly failing if another connection
        # holds a write lock (e.g. a dashboard reader doing a long SELECT).
        self._conn.execute("PRAGMA busy_timeout = 30000")
        # Disable automatic WAL checkpointing during batch backtest writes.
        # The WAL file can grow to ~1000 pages then trigger a slow OS-level
        # checkpoint mid-loop (especially on Windows with AV scanning).
        # We call checkpoint() explicitly after the backtest loop completes.
        self._conn.execute("PRAGMA wal_autocheckpoint = 0")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._trades_since_review = 0
        self._trend: TrendSnapshot = TrendSnapshot()

    def _migrate(self) -> None:
        """Apply additive schema migrations for existing databases."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        additions = {
            "bot_name":       "TEXT",
            "fill_price":     "INTEGER",
            "slippage_cents": "INTEGER",
            "fill_status":    "TEXT DEFAULT 'unknown'",
            "order_id":       "TEXT",
            "pnl_valid":      "INTEGER DEFAULT 1",
            "pnl_validation_reason": "TEXT",
            "reconciliation_trace": "TEXT",
        }
        for col, col_def in additions.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_def}")
                logger.info("Schema migration: added column trades.%s", col)

        # Normalize historical mislabeled outcomes: zero P&L is breakeven, not win.
        cur = self._conn.execute(
            """
            UPDATE trades
            SET outcome = 'breakeven'
            WHERE outcome = 'win'
              AND COALESCE(pnl_cents, 0) = 0
              AND settled_at IS NOT NULL
            """
        )
        if (cur.rowcount or 0) > 0:
            logger.info(
                "Schema migration: relabeled %d zero-PnL trade(s) from win -> breakeven.",
                cur.rowcount,
            )

    # ------------------------------------------------------------------
    # WAL checkpoint
    # ------------------------------------------------------------------

    def checkpoint(self) -> None:
        """Run a passive WAL checkpoint to flush WAL pages to the main DB file.

        Call this after bulk-write operations (e.g. after the backtest loop)
        to keep the WAL file small.  A PASSIVE checkpoint does not block
        readers or writers; it only writes pages that are not currently in use.
        """
        try:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            logger.debug("WAL checkpoint completed for %s", self.cfg.get("db_path", "db"))
        except Exception as exc:
            logger.warning("WAL checkpoint failed: %s", exc)

    # ------------------------------------------------------------------
    # Trade logging
    # ------------------------------------------------------------------

    def log_trade(
        self,
        ticker: str,
        event_ticker: str,
        title: str,
        side: str,
        action: str,
        count: int,
        entry_price: int,
        confidence: float,
        edge_score: float = 0.0,
        liquidity_score: float = 0.0,
        volume_score: float = 0.0,
        timing_score: float = 0.0,
        momentum_score: float = 0.0,
        rationale: str = "",
        series_ticker: str = "",
        category: str = "",
        bot_name: str = "",
        order_id: str = "",
        fill_price: Optional[int] = None,
        slippage_cents: Optional[int] = None,
        fill_status: str = "pending",
    ) -> int:
        """Record a new trade entry. Returns the database row ID."""
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            INSERT INTO trades
                (timestamp, ticker, event_ticker, title, series_ticker, category,
                 bot_name, side, action, count, entry_price, fill_price,
                 slippage_cents, fill_status, order_id,
                 confidence, edge_score, liquidity_score, volume_score,
                 timing_score, momentum_score, rationale, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (now, ticker, event_ticker, title, series_ticker, category,
             bot_name, side, action, count, entry_price, fill_price,
             slippage_cents, fill_status, order_id,
             confidence, edge_score, liquidity_score, volume_score,
             timing_score, momentum_score, rationale),
        )
        self._conn.commit()
        self._trades_since_review += 1
        trade_id = cur.lastrowid
        logger.info(
            "Trade logged (id=%d): %s %s %s x%d @ %d¢ [conf=%.1f cat=%s bot=%s order=%s]",
            trade_id, action, side, ticker, count, entry_price, confidence,
            category, bot_name, order_id or "n/a",
        )
        return trade_id

    def update_outcome(
        self,
        trade_id: int,
        outcome: str,
        exit_price: Optional[int] = None,
        pnl_cents: Optional[int] = None,
        pnl_valid: Optional[bool] = None,
        pnl_validation_reason: str = "",
        reconciliation_trace: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update a trade record with its final outcome."""
        now = datetime.now(timezone.utc).isoformat()
        valid_flag = 1 if pnl_valid is None else (1 if pnl_valid else 0)
        trace_text = ""
        if reconciliation_trace:
            try:
                trace_text = json.dumps(reconciliation_trace, ensure_ascii=True, sort_keys=True)
            except Exception:
                trace_text = str(reconciliation_trace)
        self._conn.execute(
            """
            UPDATE trades
            SET outcome = ?, exit_price = ?, pnl_cents = ?, settled_at = ?,
                pnl_valid = ?, pnl_validation_reason = ?, reconciliation_trace = ?
            WHERE id = ?
            """,
            (
                outcome,
                exit_price,
                pnl_cents,
                now,
                valid_flag,
                str(pnl_validation_reason or ""),
                trace_text,
                trade_id,
            ),
        )
        self._conn.commit()

        row = self._conn.execute(
            "SELECT category FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if row and row["category"] and outcome in {"win", "loss"} and valid_flag == 1:
            self._update_category_stats(row["category"], outcome, pnl_cents or 0)

        logger.info(
            "Trade %d outcome: %s (P&L: %s¢ valid=%s reason=%s)",
            trade_id, outcome, pnl_cents, bool(valid_flag), pnl_validation_reason or "ok",
        )

    def update_outcome_by_ticker(
        self,
        ticker: str,
        outcome: str,
        exit_price: Optional[int] = None,
        pnl_cents: Optional[int] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self._conn.execute(
            """
            UPDATE trades
            SET outcome = ?, exit_price = ?, pnl_cents = ?, settled_at = ?
            WHERE ticker = ? AND outcome = 'pending'
            """,
            (outcome, exit_price, pnl_cents, now, ticker),
        )
        self._conn.commit()
        return cur.rowcount

    def _update_category_stats(self, category: str, outcome: str, pnl_cents: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        win = 1 if outcome == "win" else 0
        self._conn.execute(
            """
            INSERT INTO category_stats (category, trades, wins, total_pnl_cents, last_updated)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(category) DO UPDATE SET
                trades          = trades + 1,
                wins            = wins + ?,
                total_pnl_cents = total_pnl_cents + ?,
                last_updated    = ?
            """,
            (category, win, pnl_cents, now, win, pnl_cents, now),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Performance analytics
    # ------------------------------------------------------------------

    def get_performance(self, last_n: Optional[int] = None) -> Dict[str, Any]:
        query = """
            SELECT confidence, pnl_cents, outcome, entry_price, count
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(pnl_valid, 1) = 1
            ORDER BY id DESC
        """
        if last_n:
            query += f" LIMIT {int(last_n)}"

        rows = self._conn.execute(query).fetchall()
        if not rows:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0,
                "roi_pct": 0.0, "sharpe": 0.0, "avg_confidence": 0.0,
            }

        wins = sum(1 for r in rows if r["outcome"] == "win")
        losses = sum(1 for r in rows if r["outcome"] == "loss")
        total = wins + losses
        pnls = [r["pnl_cents"] or 0 for r in rows]
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / total if total else 0.0
        avg_conf = sum(r["confidence"] for r in rows) / total if total else 0.0
        capital = sum((r["entry_price"] or 1) * (r["count"] or 1) for r in rows) or 1
        roi = (total_pnl / capital) * 100.0

        if len(pnls) > 1:
            mean_p = sum(pnls) / len(pnls)
            var = sum((p - mean_p) ** 2 for p in pnls) / (len(pnls) - 1)
            std_p = math.sqrt(var) if var > 0 else 1.0
            sharpe = mean_p / std_p
        else:
            sharpe = 0.0

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 2) if total else 0.0,
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": total_pnl,
            "roi_pct": round(roi, 2),
            "sharpe": round(sharpe, 4),
            "avg_confidence": round(avg_conf, 2),
        }

    def get_confidence_calibration(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT
                CAST(confidence / 10 AS INTEGER) * 10 AS bucket,
                COUNT(*) AS n,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(pnl_valid, 1) = 1
            GROUP BY bucket
            ORDER BY bucket
            """
        ).fetchall()
        return [
            {
                "bucket": f"{r['bucket']}\u2013{r['bucket'] + 9}",
                "bucket_mid": r["bucket"] + 5,
                "trades": r["n"],
                "wins": r["wins"],
                "win_rate": round(r["wins"] / r["n"] * 100, 1) if r["n"] else 0.0,
                "calibration_error": round(
                    (r["wins"] / r["n"] * 100) - (r["bucket"] + 5), 1
                ) if r["n"] else 0.0,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Trend detection
    # ------------------------------------------------------------------

    def compute_trend(self, window: int = 20, half_window: int = 10) -> TrendSnapshot:
        """
        Compare recent vs prior half-window of trades to detect momentum,
        hot/cold categories, calibration bias, and feature importance.
        """
        snap = TrendSnapshot()

        rows = self._conn.execute(
            """
            SELECT outcome, pnl_cents, confidence, category,
                   edge_score, liquidity_score, volume_score,
                   timing_score, momentum_score
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(pnl_valid, 1) = 1
            ORDER BY id DESC
            LIMIT ?
            """,
            (window,),
        ).fetchall()

        if len(rows) < half_window:
            self._trend = snap
            return snap

        recent = rows[:half_window]
        prior = rows[half_window:]

        def _win_rate(subset):
            if not subset:
                return 0.0
            return sum(1 for r in subset if r["outcome"] == "win") / len(subset)

        def _avg_pnl(subset):
            if not subset:
                return 0.0
            return sum(r["pnl_cents"] or 0 for r in subset) / len(subset)

        snap.win_rate_trend = _win_rate(recent) - _win_rate(prior)
        snap.pnl_trend = _avg_pnl(recent) - _avg_pnl(prior)

        # Category win rates vs overall
        cat_stats: Dict[str, Dict] = defaultdict(lambda: {"wins": 0, "total": 0})
        for r in rows:
            cat = r["category"] or "unknown"
            cat_stats[cat]["total"] += 1
            if r["outcome"] == "win":
                cat_stats[cat]["wins"] += 1

        overall_wr = _win_rate(rows)
        for cat, s in cat_stats.items():
            if s["total"] < 3:
                continue
            cat_wr = s["wins"] / s["total"]
            if cat_wr > overall_wr + 0.10:
                snap.hot_categories.append(cat)
            elif cat_wr < overall_wr - 0.10:
                snap.cold_categories.append(cat)

        # Calibration bias
        cal = self.get_confidence_calibration()
        if cal:
            errors = [b["calibration_error"] for b in cal if b["trades"] >= 3]
            snap.calibration_bias = sum(errors) / len(errors) if errors else 0.0

        # Feature importance via point-biserial correlation
        dims = ["edge_score", "liquidity_score", "volume_score", "timing_score", "momentum_score"]
        labels = ["edge", "liquidity", "volume", "timing", "momentum"]
        binary = [1.0 if r["outcome"] == "win" else 0.0 for r in rows]
        for dim, label in zip(dims, labels):
            values = [r[dim] or 0.0 for r in rows]
            snap.feature_importance[label] = self._point_biserial(values, binary)

        # Momentum multiplier: blend win rate trend and P&L trend
        trend_signal = snap.win_rate_trend * 2 + snap.pnl_trend / 200.0
        trend_min = float(self.cfg.get("trend_multiplier_min", 0.7))
        trend_max = float(self.cfg.get("trend_multiplier_max", 1.3))
        if trend_min > trend_max:
            trend_min, trend_max = trend_max, trend_min
        snap.momentum_multiplier = max(trend_min, min(trend_max, 1.0 + trend_signal))

        logger.info(
            "Trend: WR_delta=%.1f%% PnL_delta=%.1f¢ bias=%.1f hot=%s cold=%s mult=%.2f FI=%s",
            snap.win_rate_trend * 100, snap.pnl_trend, snap.calibration_bias,
            snap.hot_categories, snap.cold_categories, snap.momentum_multiplier,
            snap.feature_importance,
        )
        self._trend = snap
        return snap

    @property
    def trend(self) -> TrendSnapshot:
        return self._trend

    @staticmethod
    def _point_biserial(values: List[float], binary: List[float]) -> float:
        """Point-biserial correlation between a continuous variable and binary outcome."""
        n = len(values)
        if n < 4:
            return 0.0
        n1 = sum(binary)
        n0 = n - n1
        if n1 == 0 or n0 == 0:
            return 0.0
        mean_1 = sum(v for v, b in zip(values, binary) if b) / n1
        mean_0 = sum(v for v, b in zip(values, binary) if not b) / n0
        mean_all = sum(values) / n
        var = sum((v - mean_all) ** 2 for v in values) / n
        std = math.sqrt(var) if var > 0 else 1e-9
        rpb = ((mean_1 - mean_0) / std) * math.sqrt(n1 * n0 / (n * n))
        return round(max(-1.0, min(1.0, rpb)), 4)

    # ------------------------------------------------------------------
    # Category performance
    # ------------------------------------------------------------------

    def get_category_performance(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT category, trades, wins,
                   ROUND(CAST(wins AS REAL) / NULLIF(trades, 0) * 100, 1) AS win_rate,
                   total_pnl_cents, last_updated
            FROM category_stats
            ORDER BY win_rate DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_category_multiplier(self, category: str) -> float:
        """
        Return a confidence multiplier for a category based on its historical
        win rate vs the overall average. Range: 0.7–1.3.
        """
        row = self._conn.execute(
            "SELECT trades, wins FROM category_stats WHERE category = ?",
            (category,),
        ).fetchone()

        if not row or row["trades"] < 5:
            return 1.0

        overall = self.get_performance()
        if overall["total_trades"] < 10:
            return 1.0

        cat_wr = row["wins"] / row["trades"]
        overall_wr = overall["win_rate"] / 100.0
        multiplier = 1.0 + (cat_wr - overall_wr)
        return round(max(0.7, min(1.3, multiplier)), 3)

    # ------------------------------------------------------------------
    # Strategy review / weight recalibration (decay-weighted + FI)
    # ------------------------------------------------------------------

    def should_review(self) -> bool:
        interval = self.cfg.get("review_interval_trades", 25)
        min_trades = self.cfg.get("min_trades_for_review", 10)
        perf = self.get_performance()
        if perf["total_trades"] < min_trades:
            return False
        return self._trades_since_review >= interval

    def review_and_recalibrate(self, current_weights: Dict[str, float]) -> Dict[str, float]:
        """
        Adjust scoring weights using decay-weighted feature importance.

        Key improvements:
        - Exponential time decay (recent trades count more).
        - Point-biserial feature importance blended with mean-difference signal.
        - Includes momentum as a 5th scoring dimension.
        - Saves calibration snapshot for dashboard.
        """
        lr = self.cfg.get("learning_rate", 0.1)
        window = self.cfg.get("rolling_window", 50)
        decay = self.cfg.get("recalibration_decay", 0.95)

        rows = self._conn.execute(
            """
            SELECT outcome, edge_score, liquidity_score, volume_score,
                   timing_score, momentum_score, confidence, pnl_cents
            FROM trades
            WHERE outcome IN ('win', 'loss')
              AND COALESCE(pnl_valid, 1) = 1
            ORDER BY id DESC
            LIMIT ?
            """,
            (window,),
        ).fetchall()

        min_required = self.cfg.get("min_trades_for_review", 10)
        if len(rows) < min_required:
            logger.info("Not enough settled trades for recalibration (%d).", len(rows))
            return current_weights

        dims = ["edge", "liquidity", "volume", "timing", "momentum"]
        score_keys = ["edge_score", "liquidity_score", "volume_score", "timing_score", "momentum_score"]

        win_avgs: Dict[str, float] = {d: 0.0 for d in dims}
        loss_avgs: Dict[str, float] = {d: 0.0 for d in dims}
        win_weight = 0.0
        loss_weight = 0.0

        for i, r in enumerate(rows):
            w = decay ** i
            scores = {d: (r[sk] or 0.0) for d, sk in zip(dims, score_keys)}
            if r["outcome"] == "win":
                for d in dims:
                    win_avgs[d] += scores[d] * w
                win_weight += w
            else:
                for d in dims:
                    loss_avgs[d] += scores[d] * w
                loss_weight += w

        if win_weight == 0 or loss_weight == 0:
            logger.info("All outcomes identical — skipping recalibration.")
            return current_weights

        for d in dims:
            win_avgs[d] /= win_weight
            loss_avgs[d] /= loss_weight

        snap = self.compute_trend(window=min(window, len(rows)))
        fi = snap.feature_importance

        new_weights = dict(current_weights)
        for d in dims:
            mean_delta = (win_avgs[d] - loss_avgs[d]) / 100.0
            corr_signal = fi.get(d, 0.0)
            combined = 0.6 * mean_delta + 0.4 * corr_signal
            adjustment = lr * combined
            base = new_weights.get(d, 1.0 / len(dims))
            new_weights[d] = max(0.04, base + adjustment)

        total = sum(new_weights[d] for d in dims)
        for d in dims:
            new_weights[d] = round(new_weights[d] / total, 4)

        perf = self.get_performance(last_n=window)
        self._conn.execute(
            """
            INSERT INTO weight_history
                (timestamp, edge, liquidity, volume, timing, momentum,
                 win_rate, avg_pnl, trade_count, trigger_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                new_weights.get("edge", 0),
                new_weights.get("liquidity", 0),
                new_weights.get("volume", 0),
                new_weights.get("timing", 0),
                new_weights.get("momentum", 0),
                perf["win_rate"],
                perf["avg_pnl"],
                perf["total_trades"],
                f"auto_review wr={perf['win_rate']:.1f}%",
            ),
        )

        # Snapshot calibration
        cal = self.get_confidence_calibration()
        now_ts = datetime.now(timezone.utc).isoformat()
        for b in cal:
            self._conn.execute(
                """
                INSERT INTO calibration_log
                    (timestamp, bucket, trades, wins, observed_win_rate)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now_ts, b["bucket_mid"], b["trades"], b["wins"], b["win_rate"]),
            )

        self._conn.commit()
        self._trades_since_review = 0
        logger.info(
            "Weights recalibrated: %s | WR=%.1f%% | FI=%s",
            new_weights, perf["win_rate"], fi,
        )
        return new_weights

    # ------------------------------------------------------------------
    # Calibration helpers
    # ------------------------------------------------------------------

    def get_calibrated_threshold(self, base_threshold: float = None) -> float:
        """
        Dynamically adjust confidence threshold based on calibration bias.
        Overconfident engine -> raise threshold. Underconfident -> lower it.
        """
        base = base_threshold if base_threshold is not None else self.cfg.get("min_confidence_threshold", 65)
        bias = self._trend.calibration_bias
        adjustment = max(-10.0, min(10.0, bias * 0.5))
        return round(max(40.0, min(90.0, base + adjustment)), 1)

    # ------------------------------------------------------------------
    # Daily summary
    # ------------------------------------------------------------------

    def save_daily_summary(
        self, pnl_cents: int, trades: int, wins: int, losses: int, avg_conf: float
    ) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        self._conn.execute(
            """
            INSERT INTO daily_summary
                (date, trades, wins, losses, gross_pnl_cents, avg_confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                trades          = excluded.trades,
                wins            = excluded.wins,
                losses          = excluded.losses,
                gross_pnl_cents = excluded.gross_pnl_cents,
                avg_confidence  = excluded.avg_confidence
            """,
            (today, trades, wins, losses, pnl_cents, avg_conf),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Raw queries
    # ------------------------------------------------------------------

    def get_all_trades(self, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_summaries(self, limit: int = 90) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_weight_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM weight_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_trades(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE outcome = 'pending' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stale_pending_trades(self, min_age_hours: float = 24.0) -> List[Dict[str, Any]]:
        """Return pending trades older than min_age_hours for forced reconciliation."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=min_age_hours)).isoformat()
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE outcome = 'pending' AND timestamp < ? ORDER BY id",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_calibration_history(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM calibration_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.checkpoint()
        self._conn.close()
