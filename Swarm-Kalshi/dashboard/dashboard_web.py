"""
dashboard_web.py
================

Flask-based Command Center dashboard for the Kalshi bot swarm.

Provides a web UI at localhost:8080 with:
- Swarm overview (all 4 bots at a glance)
- Individual bot detail tabs
- Performance analytics with Chart.js
- Bot controls (start/stop/pause, budget allocation, etc.)
- Auto-refresh every 15 seconds
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
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

    def query_bot_db(bot_name: str, query: str, params: tuple = ()) -> List[Dict]:
        """Execute a query against a bot's SQLite database."""
        db_path = get_bot_db_path(bot_name)
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug("DB query error for %s: %s", bot_name, exc)
            return []

    def get_bot_performance(bot_name: str) -> Dict[str, Any]:
        """Get performance metrics for a bot."""
        rows = query_bot_db(
            bot_name,
            """
            SELECT confidence, pnl_cents, outcome, entry_price, count
            FROM trades WHERE outcome IN ('win', 'loss')
            ORDER BY id DESC
            """,
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
        return query_bot_db(
            bot_name,
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    def get_bot_daily_summaries(bot_name: str, limit: int = 30) -> List[Dict]:
        """Get daily summaries for a bot."""
        return query_bot_db(
            bot_name,
            "SELECT * FROM daily_summary ORDER BY date DESC LIMIT ?",
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
        return query_bot_db(
            bot_name,
            """
            SELECT
                CAST(confidence / 10 AS INTEGER) * 10 AS bucket,
                COUNT(*) AS n,
                SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins
            FROM trades
            WHERE outcome IN ('win', 'loss')
            GROUP BY bucket ORDER BY bucket
            """,
        )

    def get_cumulative_pnl(bot_name: str) -> List[Dict]:
        """Get cumulative P&L series for charting."""
        rows = query_bot_db(
            bot_name,
            """
            SELECT id, timestamp, pnl_cents, outcome
            FROM trades WHERE outcome IN ('win', 'loss')
            ORDER BY id ASC
            """,
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
    # Auth
    # ------------------------------------------------------------------

    _auth_cfg = swarm_cfg.get("dashboard", {}).get("auth", {})

    @app.before_request
    def require_auth():
        return _auth_required(_auth_cfg)

    # ------------------------------------------------------------------
    # Routes -- Pages
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        """Render the main dashboard page."""
        return render_template(
            "dashboard.html",
            bots=BOT_DISPLAY,
            bot_names=BOT_NAMES,
            refresh_interval=swarm_cfg.get("dashboard", {}).get("auto_refresh_seconds", 15),
        )

    # ------------------------------------------------------------------
    # Routes -- API endpoints
    # ------------------------------------------------------------------

    @app.route("/api/swarm/status")
    def api_swarm_status():
        """Get overall swarm status."""
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            return jsonify(coordinator.get_swarm_status())

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

        # Fallback: read from log files
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
    # Routes -- Control endpoints
    # ------------------------------------------------------------------

    @app.route("/api/control/start/<bot_name>", methods=["POST"])
    def api_start_bot(bot_name):
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.start_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/stop/<bot_name>", methods=["POST"])
    def api_stop_bot(bot_name):
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.stop_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/pause/<bot_name>", methods=["POST"])
    def api_pause_bot(bot_name):
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.pause_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/resume/<bot_name>", methods=["POST"])
    def api_resume_bot(bot_name):
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            success = coordinator.resume_bot(bot_name)
            return jsonify({"success": success})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/pause_all", methods=["POST"])
    def api_pause_all():
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.pause_all()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/resume_all", methods=["POST"])
    def api_resume_all():
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.resume_all()
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/control/budget", methods=["POST"])
    def api_update_budget():
        """Update budget allocation for a bot."""
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
        data = request.json or {}
        limit = data.get("limit_cents", 15000)
        coordinator = app.config.get("COORDINATOR")
        if coordinator:
            coordinator.swarm_cfg["global_daily_loss_limit_cents"] = limit
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "No coordinator"})

    @app.route("/api/alerts")
    def api_alerts():
        """
        Return active alerts for the swarm.

        Alert types:
        - bot_crashed: a bot is in error/stopped state unexpectedly
        - daily_loss_limit: global or per-bot daily loss limit hit
        - trade_failed: recent trade error in activity log
        - stale_pending: pending trades older than 48h
        """
        alerts = []
        coordinator = app.config.get("COORDINATOR")

        # -- Bot crash alerts --
        for bot_name in ["sentinel", "oracle", "pulse", "vanguard"]:
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

        # -- Daily loss limit alerts (per-bot) --
        for bot_name in ["sentinel", "oracle", "pulse", "vanguard"]:
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

        # -- Stale pending trade alerts --
        from datetime import datetime, timezone, timedelta
        for bot_name in ["sentinel", "oracle", "pulse", "vanguard"]:
            try:
                db_path = get_bot_db_path(bot_name)
                if db_path.exists():
                    import sqlite3
                    conn = sqlite3.connect(str(db_path))
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

        # -- Recent trade failure alerts from activity log --
        activity = app.config.get("ACTIVITY_LOG", [])
        for entry in activity[-20:]:
            if "error" in entry.get("message", "").lower() or entry.get("level") == "error":
                alerts.append({
                    "level": "warning",
                    "type": "trade_error",
                    "bot": entry.get("bot", "unknown"),
                    "message": entry.get("message", ""),
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
