"""
dashboard_web.py
================

Flask-based Command Center dashboard for the Kalshi bot swarm.

Provides a web UI at localhost:8080 with:
- Swarm overview (all 4 bots at a glance)
- Individual bot detail tabs
- Performance analytics with Chart.js
- Bot controls (start/stop/pause, budget allocation, etc.)
- Risk monitor (per-bot drawdown, balance, daily P&L, can_trade)
- Advanced analytics (Sharpe, Sortino, profit factor, streaks)
- Open positions table
- System tab (auto-scale changes, meta-insights, uptime)
- Auto-refresh every 15 seconds
- Background equity snapshot writer (every 5 minutes)
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import hashlib
import hmac
import secrets

import yaml
from flask import Flask, Response, jsonify, render_template, request

logger = logging.getLogger("dashboard")


def _check_auth(username: str, password: str, cfg_auth: dict) -> bool:
    """Constant-time comparison to avoid timing attacks."""
    expected_user = cfg_auth.get("username", "admin")
    expected_pass = cfg_auth.get("password", "")
    if not expected_pass:
        return False
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(
        password, expected_pass
    )


def _auth_required(cfg_auth: dict) -> Optional[Response]:
    """Return a 401 response if auth is enabled and credentials are wrong/missing."""
    if not cfg_auth.get("enabled", False):
        return None
    auth = request.authorization
    if auth and _check_auth(auth.username, auth.password, cfg_auth):
        return None
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="Kalshi Swarm Dashboard"'},
    )

# Bot definitions
BOT_NAMES = ["sentinel", "oracle", "pulse", "vanguard"]
BOT_DISPLAY = {
    "sentinel": {"name": "Sentinel", "specialist": "Politics/Elections", "color": "#e74c3c"},
    "oracle": {"name": "Oracle", "specialist": "Economics/Finance", "color": "#3498db"},
    "pulse": {"name": "Pulse", "specialist": "Climate/Weather/Science", "color": "#2ecc71"},
    "vanguard": {"name": "Vanguard", "specialist": "Culture/Tech/Crypto", "color": "#f39c12"},
}


def create_app(
    project_root: str = ".",
    coordinator=None,
) -> Flask:
    """Create and configure the Flask dashboard application."""
    project_root = Path(project_root).resolve()

    app = Flask(
        __name__,
        template_folder=str(project_root / "dashboard" / "templates"),
        static_folder=str(project_root / "dashboard" / "static"),
    )
    app.config["PROJECT_ROOT"] = str(project_root)
    app.config["COORDINATOR"] = coordinator

    # Load swarm config
    config_path = project_root / "config" / "swarm_config.yaml"
    swarm_cfg = {}
    if config_path.exists():
        with open(config_path) as fh:
            swarm_cfg = yaml.safe_load(fh) or {}

    app.config["SWARM_CONFIG"] = swarm_cfg

    # Apply env var overrides for dashboard auth
    _dash_auth = swarm_cfg.setdefault("dashboard", {}).setdefault("auth", {})
    if os.environ.get("DASHBOARD_USER"):
        _dash_auth["username"] = os.environ["DASHBOARD_USER"]
    if os.environ.get("DASHBOARD_PASS"):
        _dash_auth["password"] = os.environ["DASHBOARD_PASS"]
        _dash_auth["enabled"] = True

    # ------------------------------------------------------------------
    # Helper functions
    # ------------------------------------------------------------------

    def _sqlite_connect(db_path: Path) -> sqlite3.Connection:
        """Open SQLite with timeout settings suitable for concurrent bot writes."""
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def get_bot_db_path(bot_name: str) -> Path:
        """Get the SQLite database path for a bot."""
        bot_cfg_path = project_root / "config" / f"{bot_name}_config.yaml"
        if bot_cfg_path.exists():
            with open(bot_cfg_path) as fh:
                bot_cfg = yaml.safe_load(fh) or {}
            db_path = bot_cfg.get("learning", {}).get("db_path", f"data/{bot_name}.db")
        else:
            db_path = f"data/{bot_name}.db"
        return project_root / db_path

    _table_column_cache: Dict[tuple, set] = {}
    _table_column_lock = threading.Lock()

    def _get_table_columns(bot_name: str, table_name: str) -> set:
        """Return cached column names for a table in a bot DB."""
        db_path = get_bot_db_path(bot_name)
        cache_key = (str(db_path), table_name)
        with _table_column_lock:
            cached = _table_column_cache.get(cache_key)
            if cached is not None:
                return cached

        columns: set = set()
        if db_path.exists():
            try:
                conn = _sqlite_connect(db_path)
                rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                conn.close()
                columns = {str(r[1]) for r in rows}
            except Exception as exc:
                logger.debug("Column introspection failed for %s.%s: %s", db_path, table_name, exc)

        with _table_column_lock:
            _table_column_cache[cache_key] = columns
        return columns

    def _trade_scope(bot_name: str) -> tuple:
        """Return WHERE clause and params for bot-scoped trades queries."""
        if "bot_name" in _get_table_columns(bot_name, "trades"):
            return "bot_name = ?", (bot_name,)
        return "1=1", ()

    def query_bot_db(bot_name: str, query: str, params: tuple = ()) -> List[Dict]:
        """Execute a query against a bot's SQLite database."""
        db_path = get_bot_db_path(bot_name)
        if not db_path.exists():
            return []
        for attempt in range(2):
            conn: Optional[sqlite3.Connection] = None
            try:
                conn = _sqlite_connect(db_path)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(query, params).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() and attempt == 0:
                    time.sleep(0.2)
                    continue
                logger.debug("DB query operational error for %s: %s", bot_name, exc)
                return []
            except Exception as exc:
                logger.debug("DB query error for %s: %s", bot_name, exc)
                return []
            finally:
                if conn is not None:
                    conn.close()
        return []

    def get_bot_performance(bot_name: str) -> Dict[str, Any]:
        """Get performance metrics for a bot."""
        where_clause, where_params = _trade_scope(bot_name)
        rows = query_bot_db(
            bot_name,
            """
            SELECT confidence, pnl_cents, outcome, entry_price, count
            FROM trades WHERE """
            + where_clause
            + """ AND outcome IN ('win', 'loss')
            ORDER BY id DESC
            """,
            where_params,
        )
        if not rows:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0,
                "roi_pct": 0.0, "avg_confidence": 0.0,
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

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
            "avg_pnl": round(avg_pnl, 2),
            "total_pnl": total_pnl,
            "roi_pct": round(roi, 2),
            "avg_confidence": round(avg_conf, 1),
        }

    def get_bot_status(bot_name: str) -> Dict[str, Any]:
        """Read bot status from its status file."""
        status_file = project_root / "data" / f"{bot_name}_status.json"
        try:
            if status_file.exists():
                with open(status_file) as fh:
                    return json.load(fh)
        except Exception as exc:
            logger.warning("Failed to read status file for %s: %s", bot_name, exc)
        return {
            "bot_name": bot_name,
            "state": "unknown",
            "performance": get_bot_performance(bot_name),
        }

    def get_bot_trades(bot_name: str, limit: int = 100) -> List[Dict]:
        """Get trade history for a bot."""
        where_clause, where_params = _trade_scope(bot_name)
        return query_bot_db(
            bot_name,
            "SELECT * FROM trades WHERE "
            + where_clause
            + " ORDER BY id DESC LIMIT ?",
            (*where_params, limit),
        )

    def get_bot_daily_summaries(bot_name: str, limit: int = 30) -> List[Dict]:
        """Get daily summaries for a bot."""
        if "bot_name" in _get_table_columns(bot_name, "trades"):
            where_clause, where_params = _trade_scope(bot_name)
            return query_bot_db(
                bot_name,
                """
                SELECT
                    SUBSTR(timestamp, 1, 10) AS date,
                    COUNT(*) AS trades,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                    SUM(COALESCE(pnl_cents, 0)) AS pnl_cents,
                    ROUND(AVG(COALESCE(confidence, 0)), 1) AS avg_confidence
                FROM trades
                WHERE """
                + where_clause
                + """ AND outcome IN ('win', 'loss', 'breakeven')
                GROUP BY SUBSTR(timestamp, 1, 10)
                ORDER BY date DESC
                LIMIT ?
                """,
                (*where_params, limit),
            )
        return query_bot_db(
            bot_name,
            "SELECT date, trades, wins, losses, gross_pnl_cents AS pnl_cents, avg_confidence, notes "
            "FROM daily_summary ORDER BY date DESC LIMIT ?",
            (limit,),
        )

    def get_bot_weight_history(bot_name: str, limit: int = 50) -> List[Dict]:
        """Get weight history for a bot."""
        return query_bot_db(
            bot_name,
            "SELECT * FROM weight_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def get_bot_category_stats(bot_name: str) -> List[Dict]:
        """Get category performance for a bot."""
        if "bot_name" in _get_table_columns(bot_name, "trades"):
            where_clause, where_params = _trade_scope(bot_name)
            return query_bot_db(
                bot_name,
                """
                SELECT
                    COALESCE(NULLIF(category, ''), 'unknown') AS category,
                    COUNT(*) AS trades,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                    ROUND(
                        CAST(SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS REAL)
                        / NULLIF(COUNT(*), 0) * 100, 1
                    ) AS win_rate,
                    SUM(COALESCE(pnl_cents, 0)) AS total_pnl_cents
                FROM trades
                WHERE """
                + where_clause
                + """ AND outcome IN ('win', 'loss', 'breakeven')
                GROUP BY COALESCE(NULLIF(category, ''), 'unknown')
                ORDER BY trades DESC
                """,
                where_params,
            )
        return query_bot_db(
            bot_name,
            """
            SELECT category, trades, wins,
                   ROUND(CAST(wins AS REAL) / NULLIF(trades, 0) * 100, 1) AS win_rate,
                   total_pnl_cents
            FROM category_stats ORDER BY win_rate DESC
            """,
        )

    def get_bot_calibration(bot_name: str) -> List[Dict]:
        """Get confidence calibration for a bot."""
        where_clause, where_params = _trade_scope(bot_name)
        return query_bot_db(
            bot_name,
            """
            SELECT
                CAST(confidence / 10 AS INTEGER) * 10 AS bucket,
                COUNT(*) AS n,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins
            FROM trades
            WHERE """
            + where_clause
            + """ AND outcome IN ('win', 'loss')
            GROUP BY bucket ORDER BY bucket
            """,
            where_params,
        )

    def get_cumulative_pnl(bot_name: str) -> List[Dict]:
        """Get cumulative P&L series for charting."""
        where_clause, where_params = _trade_scope(bot_name)
        rows = query_bot_db(
            bot_name,
            """
            SELECT id, timestamp, pnl_cents, outcome
            FROM trades WHERE """
            + where_clause
            + """ AND outcome IN ('win', 'loss')
            ORDER BY id ASC
            """,
            where_params,
        )
        result = []
        running = 0
        for r in rows:
            running += r["pnl_cents"] or 0
            result.append({
                "trade_num": len(result) + 1,
                "timestamp": r["timestamp"],
                "cumulative_pnl": running,
            })
        return result

    # ------------------------------------------------------------------
    # NEW: Advanced analytics helpers
    # ------------------------------------------------------------------

    def _compute_advanced_analytics(bot_name: str) -> Dict[str, Any]:
        """Compute Sharpe, Sortino, profit factor, max drawdown, streaks, best/worst trade."""
        where_clause, where_params = _trade_scope(bot_name)
        rows = query_bot_db(
            bot_name,
            """
            SELECT pnl_cents, outcome, timestamp
            FROM trades WHERE """
            + where_clause
            + """ AND outcome IN ('win', 'loss')
            ORDER BY id ASC
            """,
            where_params,
        )

        if not rows:
            return {
                "sharpe": 0.0, "sortino": 0.0, "profit_factor": 0.0,
                "max_drawdown_pct": 0.0, "win_streak": 0, "loss_streak": 0,
                "best_trade_cents": 0, "worst_trade_cents": 0,
            }

        pnls = [float(r["pnl_cents"] or 0) for r in rows]
        outcomes = [r["outcome"] for r in rows]

        # Sharpe ratio (annualised using 252 trading periods)
        n = len(pnls)
        mean_pnl = sum(pnls) / n
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / n if n > 1 else 0.0
        std_pnl = math.sqrt(variance)
        sharpe = (mean_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0.0

        # Sortino ratio (downside deviation only)
        downside = [p for p in pnls if p < 0]
        if downside:
            ds_var = sum(p ** 2 for p in downside) / len(downside)
            ds_std = math.sqrt(ds_var)
            sortino = (mean_pnl / ds_std) * math.sqrt(252) if ds_std > 0 else 0.0
        else:
            sortino = float("inf") if mean_pnl > 0 else 0.0

        # Profit factor
        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

        # Max drawdown (from cumulative peak)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = (peak - cum) / abs(peak) * 100.0 if peak != 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        # Win/loss streaks
        win_streak = 0
        loss_streak = 0
        cur_win = 0
        cur_loss = 0
        for o in outcomes:
            if o == "win":
                cur_win += 1
                cur_loss = 0
                win_streak = max(win_streak, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                loss_streak = max(loss_streak, cur_loss)

        return {
            "sharpe": round(sharpe, 3),
            "sortino": round(min(sortino, 999.0), 3),
            "profit_factor": round(min(profit_factor, 999.0), 3),
            "max_drawdown_pct": round(max_dd, 2),
            "win_streak": win_streak,
            "loss_streak": loss_streak,
            "best_trade_cents": int(max(pnls)),
            "worst_trade_cents": int(min(pnls)),
        }

    def _get_open_positions(bot_name: str) -> List[Dict]:
        """Return pending/open trades from a bot's DB."""
        where_clause, where_params = _trade_scope(bot_name)
        rows = query_bot_db(
            bot_name,
            """
            SELECT id, timestamp, ticker, side, entry_price, count,
                   COALESCE(entry_price_cents, entry_price) AS entry_price_cents
            FROM trades
            WHERE """
            + where_clause
            + """ AND outcome = 'pending'
            ORDER BY id DESC LIMIT 100
            """,
            where_params,
        )
        now = datetime.now(timezone.utc)
        result = []
        for r in rows:
            ts = r.get("timestamp", "")
            try:
                dt_obj = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hours_held = round((now - dt_obj).total_seconds() / 3600.0, 1)
            except Exception:
                hours_held = None
            entry_cents = r.get("entry_price_cents") or r.get("entry_price") or 0
            result.append({
                "bot": bot_name,
                "ticker": r.get("ticker", ""),
                "side": r.get("side", ""),
                "entry_price_cents": int(entry_cents),
                "count": r.get("count", 0),
                "timestamp": ts,
                "hours_held": hours_held,
            })
        return result

    # ------------------------------------------------------------------
    # Equity snapshot writer (background thread)
    # ------------------------------------------------------------------

    _equity_snapshots_path = project_root / "data" / "equity_snapshots.json"
    _equity_writer_started = False
    _equity_writer_lock = threading.Lock()

    def _load_equity_snapshots() -> List[Dict]:
        try:
            if _equity_snapshots_path.exists():
                with open(_equity_snapshots_path) as fh:
                    data = json.load(fh)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _append_equity_snapshot():
        """Compute current total PnL from all bot DBs and append a snapshot."""
        try:
            per_bot_pnl: Dict[str, int] = {}
            total_pnl = 0
            for bot_name in BOT_NAMES:
                perf = get_bot_performance(bot_name)
                pnl = int(perf.get("total_pnl", 0) or 0)
                per_bot_pnl[bot_name] = pnl
                total_pnl += pnl

            snapshot = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_pnl_cents": total_pnl,
                "per_bot_pnl": per_bot_pnl,
            }

            # Read existing, append, keep last 2016 snapshots (~7 days at 5min)
            snapshots = _load_equity_snapshots()
            snapshots.append(snapshot)
            if len(snapshots) > 2016:
                snapshots = snapshots[-2016:]

            _equity_snapshots_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = _equity_snapshots_path.with_suffix(".tmp")
            with open(tmp_path, "w") as fh:
                json.dump(snapshots, fh)
            tmp_path.replace(_equity_snapshots_path)
        except Exception as exc:
            logger.debug("Equity snapshot write failed: %s", exc)

    def _equity_writer_thread():
        """Background thread: append equity snapshot every 5 minutes."""
        INTERVAL = 300
        while True:
            try:
                time.sleep(INTERVAL)
                _append_equity_snapshot()
            except Exception:
                pass

    def _start_equity_writer():
        nonlocal _equity_writer_started
        with _equity_writer_lock:
            if not _equity_writer_started:
                _equity_writer_started = True
                t = threading.Thread(target=_equity_writer_thread, daemon=True, name="equity-writer")
                t.start()

    _start_equity_writer()

    # ------------------------------------------------------------------
    # LLM context helpers
    # ------------------------------------------------------------------

    def get_recent_central_llm_decisions(limit: int = 12) -> List[Dict[str, Any]]:
        """Read recent centralized LLM trade decisions from shared DB."""
        central_cfg = swarm_cfg.get("central_llm", {}) if isinstance(swarm_cfg, dict) else {}
        db_rel = str(central_cfg.get("db_path", "data/central_llm_controller.db"))
        db_path = project_root / db_rel
        if not db_path.exists():
            return []
        try:
            conn = _sqlite_connect(db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT timestamp, bot_name, ticker, side, quant_confidence, llm_confidence,
                       decision, size_multiplier, rationale, red_flags
                FROM llm_decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.debug("Failed reading central_llm decisions: %s", exc)
            return []

        out: List[Dict[str, Any]] = []
        for row in rows:
            red_flags = row["red_flags"]
            try:
                red_flags = json.loads(red_flags) if red_flags else []
            except Exception:
                red_flags = [str(red_flags)] if red_flags else []
            out.append({
                "timestamp": row["timestamp"],
                "bot_name": row["bot_name"],
                "ticker": row["ticker"],
                "side": row["side"],
                "quant_confidence": row["quant_confidence"],
                "llm_confidence": row["llm_confidence"],
                "decision": row["decision"],
                "size_multiplier": row["size_multiplier"],
                "rationale": row["rationale"],
                "red_flags": red_flags,
            })
        return out

    def get_recent_runtime_events(max_lines: int = 250, per_bot: int = 8) -> Dict[str, List[str]]:
        """Read recent bot runtime events from swarm log for operator context."""
        log_path = project_root / "logs" / "swarm.log"
        events: Dict[str, List[str]] = {bot: [] for bot in BOT_NAMES}
        if not log_path.exists():
            return events

        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            logger.debug("Failed reading swarm log: %s", exc)
            return events

        tail = lines[-max_lines:]
        for line in tail:
            line_stripped = line.strip()
            for bot in BOT_NAMES:
                marker = f"| {bot} |"
                if marker in line_stripped:
                    parts = [p.strip() for p in line_stripped.split("|")]
                    message = parts[-1] if parts else line_stripped
                    if message and (not events[bot] or events[bot][-1] != message):
                        events[bot].append(message)

        for bot in BOT_NAMES:
            if len(events[bot]) > per_bot:
                events[bot] = events[bot][-per_bot:]
        return events

    def get_swarm_context_for_chat(
        max_trades_per_bot: int = 6,
        max_central_decisions: int = 12,
    ) -> Dict[str, Any]:
        """Build compact live context for interactive operator chat."""
        bots: Dict[str, Any] = {}
        runtime_events = get_recent_runtime_events()
        total_trades = 0
        total_wins = 0
        total_pnl = 0

        for bot_name in BOT_NAMES:
            status = get_bot_status(bot_name)
            perf = get_bot_performance(bot_name)
            risk = status.get("risk", {}) if isinstance(status, dict) else {}
            trades = get_bot_trades(bot_name, limit=max_trades_per_bot)

            recent_trades = []
            for t in trades:
                recent_trades.append({
                    "timestamp": t.get("timestamp"),
                    "ticker": t.get("ticker"),
                    "side": t.get("side"),
                    "entry_price": t.get("entry_price"),
                    "count": t.get("count"),
                    "confidence": t.get("confidence"),
                    "outcome": t.get("outcome"),
                    "pnl_cents": t.get("pnl_cents"),
                    "rationale": t.get("rationale"),
                })

            bots[bot_name] = {
                "state": status.get("state", "unknown"),
                "timestamp": status.get("timestamp"),
                "pid": status.get("pid"),
                "session_start": status.get("session_start"),
                "session_trade_count": status.get("trade_count", 0),
                "pending_trades": status.get("pending_trades", 0),
                "recent_runtime_events": runtime_events.get(bot_name, []),
                "risk": {
                    "balance_cents": risk.get("balance_cents", 0),
                    "daily_pnl_cents": risk.get("daily_pnl_cents", 0),
                    "drawdown_pct": risk.get("drawdown_pct", 0.0),
                    "open_positions": risk.get("open_positions", 0),
                    "can_trade": risk.get("can_trade", False),
                    "consecutive_losses": risk.get("consecutive_losses", 0),
                },
                "performance": {
                    "total_trades": perf.get("total_trades", 0),
                    "wins": perf.get("wins", 0),
                    "losses": perf.get("losses", 0),
                    "win_rate": perf.get("win_rate", 0.0),
                    "total_pnl": perf.get("total_pnl", 0),
                    "avg_confidence": perf.get("avg_confidence", 0.0),
                },
                "recent_trades": recent_trades,
            }

            total_trades += int(perf.get("total_trades", 0) or 0)
            total_wins += int(perf.get("wins", 0) or 0)
            total_pnl += int(perf.get("total_pnl", 0) or 0)

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "global_metrics": {
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_losses": max(0, total_trades - total_wins),
                "overall_win_rate": round((total_wins / total_trades * 100), 1) if total_trades > 0 else 0.0,
                "total_pnl_cents": total_pnl,
            },
            "bots": bots,
            "recent_central_llm_decisions": get_recent_central_llm_decisions(limit=max_central_decisions),
        }

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    _auth_cfg = swarm_cfg.get("dashboard", {}).get("auth", {})

    def _manual_controls_locked() -> bool:
        auto_cfg = swarm_cfg.get("autonomous_mode", {}) if isinstance(swarm_cfg, dict) else {}
        return bool(auto_cfg.get("enabled", False) and auto_cfg.get("lock_manual_controls", False))

    def _manual_controls_forbidden():
        return jsonify({
            "success": False,
            "error": "Manual controls are locked by autonomous_mode.",
        }), 403

    @app.before_request
    def require_auth():
        return _auth_required(_auth_cfg)

    # ------------------------------------------------------------------
    # Routes -- Pages
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        """Render the main dashboard page."""
        central_cfg = swarm_cfg.get("central_llm", {}) if isinstance(swarm_cfg, dict) else {}
        auto_cfg = swarm_cfg.get("autonomous_mode", {}) if isinstance(swarm_cfg, dict) else {}
        provider = str(central_cfg.get("provider", "anthropic")).strip().lower()
        default_chat_model = (
            str(central_cfg.get("anthropic_model") or central_cfg.get("model") or "claude-3-5-haiku-latest")
            if provider in {"anthropic", "claude"}
            else str(central_cfg.get("model", "qwen2.5:14b"))
        )
        manual_controls_locked = bool(
            auto_cfg.get("enabled", False) and auto_cfg.get("lock_manual_controls", False)
        )
        return render_template(
            "dashboard.html",
            bots=BOT_DISPLAY,
            bot_names=BOT_NAMES,
            refresh_interval=swarm_cfg.get("dashboard", {}).get(
                "refresh_interval_seconds",
                swarm_cfg.get("dashboard", {}).get("auto_refresh_seconds", 15),
            ),
            ollama_model=default_chat_model,
            manual_controls_locked=manual_controls_locked,
        )

    # ------------------------------------------------------------------
    # Routes -- API endpoints (existing)
    # ------------------------------------------------------------------

    @app.route("/api/swarm/status")
    def api_swarm_status():
        """Get overall swarm status."""
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            status = coordinator.get_swarm_status()
            bots = status.get("bots", {}) if isinstance(status, dict) else {}
            for bot_name in BOT_NAMES:
                bot_status = bots.get(bot_name, {}) if isinstance(bots, dict) else {}
                if not isinstance(bot_status, dict):
                    bot_status = {}
                bot_status["performance"] = get_bot_performance(bot_name)
                bots[bot_name] = bot_status

            total_trades = sum(
                int((bots.get(name, {}).get("performance", {}) or {}).get("total_trades", 0) or 0)
                for name in BOT_NAMES
            )
            total_wins = sum(
                int((bots.get(name, {}).get("performance", {}) or {}).get("wins", 0) or 0)
                for name in BOT_NAMES
            )
            total_pnl = sum(
                int((bots.get(name, {}).get("performance", {}) or {}).get("total_pnl", 0) or 0)
                for name in BOT_NAMES
            )

            status["bots"] = bots
            status["global_metrics"] = {
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_losses": total_trades - total_wins,
                "overall_win_rate": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0,
                "total_pnl_cents": total_pnl,
                "total_pnl_dollars": round(total_pnl / 100, 2),
            }
            return jsonify(status)

        # Fallback: read from status files
        bots = {}
        for bot_name in BOT_NAMES:
            status = get_bot_status(bot_name)
            perf = get_bot_performance(bot_name)
            status["performance"] = perf
            bots[bot_name] = status

        total_trades = sum(b["performance"]["total_trades"] for b in bots.values())
        total_wins = sum(b["performance"]["wins"] for b in bots.values())
        total_pnl = sum(b["performance"]["total_pnl"] for b in bots.values())

        return jsonify({
            "swarm_state": "running",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bots": bots,
            "global_metrics": {
                "total_trades": total_trades,
                "total_wins": total_wins,
                "total_losses": total_trades - total_wins,
                "overall_win_rate": round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0,
                "total_pnl_cents": total_pnl,
                "total_pnl_dollars": round(total_pnl / 100, 2),
            },
        })

    @app.route("/api/bot/<bot_name>/performance")
    def api_bot_performance(bot_name):
        """Get performance metrics for a specific bot."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        return jsonify(get_bot_performance(bot_name))

    @app.route("/api/bot/<bot_name>/trades")
    def api_bot_trades(bot_name):
        """Get trade history for a specific bot."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        limit = request.args.get("limit", 100, type=int)
        return jsonify(get_bot_trades(bot_name, limit))

    @app.route("/api/bot/<bot_name>/daily")
    def api_bot_daily(bot_name):
        """Get daily summaries for a specific bot."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        return jsonify(get_bot_daily_summaries(bot_name))

    @app.route("/api/bot/<bot_name>/weights")
    def api_bot_weights(bot_name):
        """Get weight history for a specific bot."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        return jsonify(get_bot_weight_history(bot_name))

    @app.route("/api/bot/<bot_name>/categories")
    def api_bot_categories(bot_name):
        """Get category performance for a specific bot."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        return jsonify(get_bot_category_stats(bot_name))

    @app.route("/api/bot/<bot_name>/calibration")
    def api_bot_calibration(bot_name):
        """Get confidence calibration for a specific bot."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        return jsonify(get_bot_calibration(bot_name))

    @app.route("/api/bot/<bot_name>/cumulative_pnl")
    def api_bot_cumulative_pnl(bot_name):
        """Get cumulative P&L series for charting."""
        if bot_name not in BOT_NAMES:
            return jsonify({"error": "Unknown bot"}), 404
        return jsonify(get_cumulative_pnl(bot_name))

    @app.route("/api/performance/all")
    def api_all_performance():
        """Get performance for all bots."""
        result = {}
        for bot_name in BOT_NAMES:
            result[bot_name] = {
                "performance": get_bot_performance(bot_name),
                "cumulative_pnl": get_cumulative_pnl(bot_name),
                "categories": get_bot_category_stats(bot_name),
                "daily": get_bot_daily_summaries(bot_name),
            }
        return jsonify(result)

    @app.route("/api/activity")
    def api_activity():
        """Get recent activity log."""
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            return jsonify(coordinator.get_activity_log(limit=50))

        activities = []
        for bot_name in BOT_NAMES:
            status = get_bot_status(bot_name)
            activities.append({
                "timestamp": status.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "bot": bot_name,
                "action": status.get("state", "unknown"),
                "detail": "",
            })
        return jsonify(activities)

    # ------------------------------------------------------------------
    # Routes -- NEW API endpoints
    # ------------------------------------------------------------------

    @app.route("/api/risk")
    def api_risk():
        """Per-bot risk cards: drawdown, balance, daily P&L, can_trade, consecutive losses, open positions."""
        result: Dict[str, Any] = {}
        global_daily_loss = 0
        global_exposure = 0

        coordinator = app.config.get("COORDINATOR")
        coordinator_cfg = swarm_cfg if isinstance(swarm_cfg, dict) else {}

        for bot_name in BOT_NAMES:
            status = get_bot_status(bot_name)
            risk = status.get("risk", {}) if isinstance(status, dict) else {}
            if not isinstance(risk, dict):
                risk = {}

            balance_cents = int(risk.get("balance_cents", 0) or 0)
            daily_pnl_cents = int(risk.get("daily_pnl_cents", 0) or 0)
            drawdown_pct = float(risk.get("drawdown_pct", 0.0) or 0.0)
            can_trade = bool(risk.get("can_trade", True))
            consecutive_losses = int(risk.get("consecutive_losses", 0) or 0)
            open_positions = int(risk.get("open_positions", 0) or 0)

            global_daily_loss += daily_pnl_cents
            global_exposure += balance_cents

            result[bot_name] = {
                "balance_cents": balance_cents,
                "balance_dollars": round(balance_cents / 100.0, 2),
                "daily_pnl_cents": daily_pnl_cents,
                "daily_pnl_dollars": round(daily_pnl_cents / 100.0, 2),
                "drawdown_pct": round(drawdown_pct, 2),
                "can_trade": can_trade,
                "consecutive_losses": consecutive_losses,
                "open_positions": open_positions,
            }

        # Global risk limits from config
        global_loss_limit_cents = int(coordinator_cfg.get("global_daily_loss_limit_cents", 15000))
        global_exposure_limit_cents = int(coordinator_cfg.get("global_exposure_limit_cents", 50000))

        result["_global"] = {
            "global_daily_pnl_cents": global_daily_loss,
            "global_daily_pnl_dollars": round(global_daily_loss / 100.0, 2),
            "global_daily_loss_limit_cents": global_loss_limit_cents,
            "global_daily_loss_limit_dollars": round(global_loss_limit_cents / 100.0, 2),
            "global_exposure_cents": global_exposure,
            "global_exposure_dollars": round(global_exposure / 100.0, 2),
            "global_exposure_limit_cents": global_exposure_limit_cents,
            "global_exposure_limit_dollars": round(global_exposure_limit_cents / 100.0, 2),
            "loss_limit_used_pct": round(abs(global_daily_loss) / global_loss_limit_cents * 100, 1) if global_loss_limit_cents > 0 else 0.0,
        }

        return jsonify(result)

    @app.route("/api/equity-curve")
    def api_equity_curve():
        """Return time series of total PnL. Reads from equity_snapshots.json or falls back to DB aggregation."""
        snapshots = _load_equity_snapshots()
        if snapshots:
            # Return last 288 snapshots (24h at 5min intervals)
            recent = snapshots[-288:]
            return jsonify({
                "source": "equity_snapshots",
                "points": recent,
                "count": len(recent),
            })

        # Fallback: aggregate per-bot cumulative PnL and sum
        combined: Dict[int, int] = {}
        max_len = 0
        for bot_name in BOT_NAMES:
            series = get_cumulative_pnl(bot_name)
            max_len = max(max_len, len(series))
            for i, pt in enumerate(series):
                combined[i] = combined.get(i, 0) + int(pt.get("cumulative_pnl", 0) or 0)

        points = [
            {"timestamp": None, "trade_num": i + 1, "total_pnl_cents": combined.get(i, 0)}
            for i in range(max_len)
        ]
        return jsonify({
            "source": "db_aggregation",
            "points": points,
            "count": len(points),
        })

    @app.route("/api/analytics/advanced")
    def api_analytics_advanced():
        """Advanced analytics: Sharpe, Sortino, profit factor, max drawdown, streaks, best/worst trade."""
        result = {}
        for bot_name in BOT_NAMES:
            result[bot_name] = _compute_advanced_analytics(bot_name)
        return jsonify(result)

    @app.route("/api/meta-insights")
    def api_meta_insights():
        """Read data/swarm_meta_insights.json and return its contents."""
        insights_path = project_root / "data" / "swarm_meta_insights.json"
        try:
            if insights_path.exists():
                with open(insights_path) as fh:
                    data = json.load(fh)
                return jsonify({"success": True, "insights": data})
        except Exception as exc:
            logger.debug("Failed reading meta insights: %s", exc)
        return jsonify({"success": False, "insights": {}, "error": "File not found or unreadable"})

    @app.route("/api/position-counts")
    def api_position_counts():
        """Return position count reconciliation from coordinator.get_swarm_status()."""
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            try:
                status = coordinator.get_swarm_status()
                pos_counts = status.get("position_counts", {})
                return jsonify({"success": True, "position_counts": pos_counts})
            except Exception as exc:
                logger.debug("Failed getting position counts: %s", exc)

        # Fallback: count pending rows per bot
        counts: Dict[str, Any] = {}
        total_pending = 0
        for bot_name in BOT_NAMES:
            where_clause, where_params = _trade_scope(bot_name)
            rows = query_bot_db(
                bot_name,
                "SELECT COUNT(*) AS cnt FROM trades WHERE " + where_clause + " AND outcome = 'pending'",
                where_params,
            )
            cnt = int((rows[0].get("cnt", 0) if rows else 0) or 0)
            counts[bot_name] = cnt
            total_pending += cnt

        return jsonify({
            "success": True,
            "position_counts": {
                "per_bot": counts,
                "total_pending": total_pending,
                "source": "db_fallback",
            },
        })

    @app.route("/api/positions")
    def api_positions():
        """Return all open/pending positions across all bots."""
        all_positions: List[Dict] = []
        for bot_name in BOT_NAMES:
            all_positions.extend(_get_open_positions(bot_name))
        all_positions.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return jsonify(all_positions)

    @app.route("/api/system")
    def api_system():
        """Return system-level info: auto-scale changes, meta-insights, uptime, conflicts."""
        coordinator = app.config.get("COORDINATOR")
        auto_scale = {}
        conflicts = {}
        uptime_seconds = None

        if coordinator:
            try:
                status = coordinator.get_swarm_status()
                auto_scale = status.get("auto_scale", {})
                conflicts = status.get("conflicts", {})
            except Exception as exc:
                logger.debug("System status fetch failed: %s", exc)

            try:
                for bot_name in BOT_NAMES:
                    bot = coordinator.bots.get(bot_name)
                    if bot and bot.started_at:
                        started = bot.started_at
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                        uptime_seconds = int((datetime.now(timezone.utc) - started).total_seconds())
                        break
            except Exception:
                pass

        # Meta-insights summary
        meta_insights = {}
        insights_path = project_root / "data" / "swarm_meta_insights.json"
        try:
            if insights_path.exists():
                with open(insights_path) as fh:
                    meta_insights = json.load(fh)
        except Exception:
            pass

        return jsonify({
            "auto_scale": auto_scale,
            "conflicts": conflicts,
            "meta_insights": meta_insights,
            "uptime_seconds": uptime_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    @app.route("/api/llm/chat", methods=["POST"])
    def api_llm_chat():
        """Send a prompt to the configured central LLM provider and return the response."""
        data = request.json or {}
        prompt = str(data.get("prompt", "")).strip()
        system_prompt = str(data.get("system", "")).strip()
        model_override = str(data.get("model", "")).strip()
        include_context = bool(data.get("include_context", True))
        raw_history = data.get("history", [])

        if not prompt:
            return jsonify({"success": False, "error": "prompt is required"}), 400

        central_cfg = swarm_cfg.get("central_llm", {}) if isinstance(swarm_cfg, dict) else {}
        provider = str(central_cfg.get("provider", "anthropic")).strip().lower()
        if provider in {"anthropic", "claude"}:
            model = model_override or str(
                central_cfg.get("anthropic_model")
                or central_cfg.get("model")
                or "claude-3-5-haiku-latest"
            )
        else:
            model = model_override or str(central_cfg.get("model", "qwen2.5:14b"))
        timeout_seconds = int(data.get("timeout_seconds") or max(60, int(central_cfg.get("timeout_seconds", 20))))
        timeout_seconds = max(10, min(timeout_seconds, 180))

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt[:4000]})
        else:
            messages.append({
                "role": "system",
                "content": (
                    "You are SwarmOps, an operator console assistant for a live trading swarm.\n"
                    "Rules:\n"
                    "1) Start with a direct answer to the user's question.\n"
                    "2) Use LIVE_SWARM_CONTEXT_JSON as primary evidence and cite exact bot names, fields, and event phrases.\n"
                    "3) Do NOT give generic suggestions like 'check logs/config' unless data is truly missing.\n"
                    "4) If something is unknown, explicitly say what field is missing from live context.\n"
                    "5) Keep concise and operational (answer + key evidence + concrete next action).\n"
                ),
            })

        context_obj = None
        if include_context:
            context_obj = get_swarm_context_for_chat()
            messages.append({
                "role": "system",
                "content": f"LIVE_SWARM_CONTEXT_JSON:\n{json.dumps(context_obj, ensure_ascii=True)}",
            })

        if isinstance(raw_history, list):
            for item in raw_history[-20:]:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role", "")).strip().lower()
                content = str(item.get("content", "")).strip()
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content[:6000]})

        messages.append({"role": "user", "content": prompt[:12000]})

        start = time.monotonic()
        if provider in {"anthropic", "claude"}:
            anthropic_api_url = str(
                central_cfg.get("anthropic_api_url", "https://api.anthropic.com/v1/messages")
            ).strip()
            anthropic_key = str(central_cfg.get("anthropic_api_key", "")).strip() or str(
                os.environ.get("ANTHROPIC_API_KEY", "")
            ).strip()
            if not anthropic_key:
                return jsonify({
                    "success": False,
                    "error": "Anthropic API key missing (set central_llm.anthropic_api_key or ANTHROPIC_API_KEY).",
                }), 500

            system_parts = [m.get("content", "") for m in messages if m.get("role") == "system"]
            anthropic_messages = [
                {"role": m.get("role"), "content": m.get("content", "")}
                for m in messages
                if m.get("role") in ("user", "assistant")
            ]
            payload = json.dumps({
                "model": model,
                "max_tokens": int(data.get("max_tokens") or central_cfg.get("max_tokens", 350)),
                "temperature": float(data.get("temperature", 0.0)),
                "system": "\n\n".join(system_parts)[:20000],
                "messages": anthropic_messages,
            }).encode("utf-8")
            req = urllib.request.Request(
                anthropic_api_url,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                logger.warning("Anthropic HTTP error %s: %s", exc.code, detail)
                return jsonify({"success": False, "error": f"Anthropic HTTP {exc.code}", "detail": detail}), 502
            except urllib.error.URLError as exc:
                return jsonify({"success": False, "error": f"Anthropic unavailable: {exc}"}), 502
            except json.JSONDecodeError:
                return jsonify({"success": False, "error": "Invalid JSON from Anthropic"}), 502

            content_blocks = body.get("content", []) if isinstance(body, dict) else []
            text_parts: List[str] = []
            if isinstance(content_blocks, list):
                for block in content_blocks:
                    if isinstance(block, dict) and str(block.get("type", "")) == "text":
                        text_parts.append(str(block.get("text", "")))
            content = "\n".join(text_parts).strip()
            if not content:
                return jsonify({"success": False, "error": "Empty response from Anthropic"}), 502
        else:
            ollama_base_url = str(central_cfg.get("ollama_base_url", "http://127.0.0.1:11434")).rstrip("/")
            payload = json.dumps({
                "model": model,
                "stream": False,
                "messages": messages,
                "options": {"temperature": 0.0},
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{ollama_base_url}/api/chat",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                logger.warning("Ollama HTTP error %s: %s", exc.code, detail)
                return jsonify({"success": False, "error": f"Ollama HTTP {exc.code}", "detail": detail}), 502
            except urllib.error.URLError as exc:
                return jsonify({"success": False, "error": f"Ollama unavailable: {exc}"}), 502
            except json.JSONDecodeError:
                return jsonify({"success": False, "error": "Invalid JSON from Ollama"}), 502

            content = str((body.get("message") or {}).get("content") or "").strip()
            if not content:
                return jsonify({"success": False, "error": "Empty response from Ollama"}), 502

        latency_ms = int((time.monotonic() - start) * 1000)
        context_summary = None
        if context_obj is not None:
            bot_states = {
                bot: details.get("state", "unknown")
                for bot, details in context_obj.get("bots", {}).items()
            }
            context_summary = {
                "generated_at": context_obj.get("generated_at"),
                "bot_states": bot_states,
                "recent_central_decisions": len(context_obj.get("recent_central_llm_decisions", [])),
                "total_trades": context_obj.get("global_metrics", {}).get("total_trades", 0),
            }

        return jsonify({
            "success": True,
            "provider": provider,
            "model": model,
            "response": content,
            "latency_ms": latency_ms,
            "context_summary": context_summary,
        })

    # Keep the /api/ollama/chat alias for backward compatibility with existing HTML
    @app.route("/api/ollama/chat", methods=["POST"])
    def api_ollama_chat_compat():
        """Backward-compatible alias for /api/llm/chat."""
        return api_llm_chat()

    # ------------------------------------------------------------------
    # Routes -- Control endpoints
    # ------------------------------------------------------------------

    @app.route("/api/control/start/<bot_name>", methods=["POST"])
    def api_start_bot(bot_name):
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.start_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/stop/<bot_name>", methods=["POST"])
    def api_stop_bot(bot_name):
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.stop_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/pause/<bot_name>", methods=["POST"])
    def api_pause_bot(bot_name):
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.pause_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/resume/<bot_name>", methods=["POST"])
    def api_resume_bot(bot_name):
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.resume_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/pause_all", methods=["POST"])
    def api_pause_all():
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.pause_all()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/resume_all", methods=["POST"])
    def api_resume_all():
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.resume_all()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/stop_all", methods=["POST"])
    def api_stop_all():
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.stop_all()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/budget", methods=["POST"])
    def api_update_budget():
        """Update budget allocation for a bot."""
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        data = request.json or {}
        bot_name = data.get("bot_name")
        pct = data.get("percentage", 0)
        coordinator = app.config.get("COORDINATOR")
        if coordinator and bot_name:
            coordinator.balance_manager.set_bot_allocation(bot_name, pct / 100.0)
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator or invalid bot"})

    @app.route("/api/control/global_loss_limit", methods=["POST"])
    def api_update_loss_limit():
        """Update global daily loss limit."""
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        data = request.json or {}
        limit = data.get("limit_cents", 15000)
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.swarm_cfg["global_daily_loss_limit_cents"] = limit
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/reset_data", methods=["POST"])
    def api_reset_data():
        """Delete all bot databases to start fresh."""
        if _manual_controls_locked():
            return _manual_controls_forbidden()
        import glob as _glob
        project_root_path = Path(app.config.get("PROJECT_ROOT", "."))
        data_dir = project_root_path / "data"
        deleted = []
        errors = []
        patterns = ["sentinel", "oracle", "pulse", "vanguard"]
        for bot in patterns:
            for path in _glob.glob(str(data_dir / f"{bot}.db*")):
                try:
                    Path(path).unlink()
                    deleted.append(Path(path).name)
                except Exception as exc:
                    errors.append(f"{Path(path).name}: {exc}")
        return jsonify({"success": not errors, "deleted": deleted, "errors": errors})

    @app.route("/api/alerts")
    def api_alerts():
        """Return active alerts for the swarm."""
        alerts = []
        coordinator = app.config.get("COORDINATOR")

        # Bot crash alerts
        for bot_name in BOT_NAMES:
            status = get_bot_status(bot_name)
            state = status.get("state", "unknown")
            if state in ("error", "stopped") and status.get("pid"):
                alerts.append({
                    "level": "critical",
                    "type": "bot_crashed",
                    "bot": bot_name,
                    "message": f"{bot_name} is {state}: {status.get('error', '')}",
                    "timestamp": status.get("timestamp", ""),
                })

        # Daily loss limit alerts (per-bot)
        for bot_name in BOT_NAMES:
            status = get_bot_status(bot_name)
            risk = status.get("risk", {})
            daily_pnl = risk.get("daily_pnl_cents", 0)
            can_trade = risk.get("can_trade", True)
            if not can_trade and daily_pnl < 0:
                alerts.append({
                    "level": "warning",
                    "type": "daily_loss_limit",
                    "bot": bot_name,
                    "message": f"{bot_name} hit daily loss limit (P&L: {daily_pnl/100:.2f}$)",
                    "timestamp": status.get("timestamp", ""),
                })

        # Stale pending trade alerts
        for bot_name in BOT_NAMES:
            try:
                db_path = get_bot_db_path(bot_name)
                if db_path.exists():
                    conn = _sqlite_connect(db_path)
                    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
                    rows = conn.execute(
                        "SELECT count(*) FROM trades WHERE outcome='pending' AND timestamp < ?",
                        (cutoff,)
                    ).fetchone()
                    conn.close()
                    if rows and rows[0] > 0:
                        alerts.append({
                            "level": "warning",
                            "type": "stale_pending",
                            "bot": bot_name,
                            "message": f"{bot_name} has {rows[0]} pending trade(s) older than 48h",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
            except Exception as exc:
                logger.debug("Alert check failed for %s: %s", bot_name, exc)

        # Recent trade failure alerts from activity log
        if coordinator:
            activity = coordinator.get_activity_log(limit=20)
        else:
            activity = app.config.get("ACTIVITY_LOG", [])
        for entry in activity[-20:]:
            action = str(entry.get("action", "")).lower()
            detail = str(entry.get("detail", "")).lower()
            msg = str(entry.get("message", "")).lower()
            level = str(entry.get("level", "")).lower()
            if (
                action in {"crashed", "failed", "error"}
                or "error" in detail
                or "exception" in detail
                or "error" in msg
                or level == "error"
            ):
                alerts.append({
                    "level": "warning",
                    "type": "trade_error",
                    "bot": entry.get("bot", "unknown"),
                    "message": entry.get("detail") or entry.get("message", ""),
                    "timestamp": entry.get("timestamp", ""),
                })

        return jsonify({"alerts": alerts, "count": len(alerts)})

    return app


def main():
    """Run the dashboard standalone."""
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Swarm Command Center")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    app = create_app(project_root=args.project_root)
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
