"""
Kalshi Swarm Dashboard — Flask backend
Port 8888 (default). Standalone — no coordinator object needed.
Reads data files directly from the project root.

Usage:
    python server.py [--port 8888] [--host 0.0.0.0] [--project-root /path/to/root]
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone, date
from functools import wraps
from pathlib import Path

import yaml
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")

# Set at startup via argparse
PROJECT_ROOT: Path = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
START_TIME: float = time.time()

BOTS = ["sentinel", "oracle", "pulse", "vanguard"]


def _load_env_file():
    """Load key-value pairs from .env if present into os.environ."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


_load_env_file()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cors(response):
    """Attach permissive CORS header to a Response object."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def json_response(data, status=200):
    """Return a JSON response with CORS headers."""
    resp = jsonify(data)
    resp.status_code = status
    return cors(resp)


def after_request_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


app.after_request(after_request_cors)


def data_path(*parts) -> Path:
    """Resolve a path inside PROJECT_ROOT/data/."""
    return PROJECT_ROOT / "data" / Path(*parts)


def config_path(*parts) -> Path:
    return PROJECT_ROOT / "config" / Path(*parts)


def log_path() -> Path:
    return PROJECT_ROOT / "logs" / "swarm.log"


def read_json(path: Path, default=None):
    """Read a JSON file, return default on any error."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default if default is not None else {}


def write_json(path: Path, data: dict):
    """Write JSON to a file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def load_yaml(filename_or_path) -> dict:
    """Read a YAML file, return empty dict on error."""
    p = Path(filename_or_path)
    if not p.is_absolute():
        p = config_path(filename_or_path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def read_yaml(path: Path) -> dict:
    return load_yaml(path)


def open_db_readonly(db_path: Path):
    """
    Open a SQLite database in WAL mode with a short timeout.
    Returns (conn, None) on success or (None, error_string) on failure.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn, None
    except Exception as exc:
        return None, str(exc)


def read_risk_state(bot: str) -> dict:
    """
    Read {bot}_risk_state.json.
    Real field names: current_balance_cents, daily.gross_pnl_cents,
                      daily.trades_today, drawdown_pause_until, peak_balance_cents.
    Returns a normalised dict with the keys the spec expects.
    """
    raw = read_json(data_path(f"{bot}_risk_state.json"), {})
    daily = raw.get("daily", {})
    bal = raw.get("current_balance_cents", 0)

    try:
        swarm_cfg = read_yaml(PROJECT_ROOT / "config" / "swarm_config.yaml") or {}
        demo_bankroll = int(swarm_cfg.get("trading", {}).get("demo_bankroll_cents", 0) or 0)
        is_demo = swarm_cfg.get("api", {}).get("demo_mode", True)
        if is_demo and demo_bankroll > 0:
            pnl = daily.get("gross_pnl_cents", 0)
            bal = max(demo_bankroll + pnl, bal)
    except Exception:
        pass

    return {
        "balance_cents":       bal,
        "daily_pnl_cents":     daily.get("gross_pnl_cents", 0),
        "daily_trades":        daily.get("trades_today", 0),
        "pause_until":         raw.get("drawdown_pause_until", None),
        "peak_balance_cents":  max(bal, raw.get("peak_balance_cents", 0)),
        "open_positions":      raw.get("open_position_count", 0),
    }


def read_status(bot: str) -> dict:
    """Read {bot}_status.json; return {} if missing."""
    raw = read_json(data_path(f"{bot}_status.json"), {})
    risk_block = raw.get("risk", {})
    return {
        "state":         raw.get("state", "unknown"),
        "can_trade":     risk_block.get("can_trade", True),
        "balance_cents": risk_block.get("balance_cents", 0),
        "open_positions": risk_block.get("open_positions", 0),
    }


def is_paused(pause_until) -> bool:
    """Return True if pause_until is a non-null, non-'none' value."""
    if pause_until is None:
        return False
    if isinstance(pause_until, str) and pause_until.lower() in ("none", "null", ""):
        return False
    return True


def bot_process_running(bot_name: str) -> bool:
    """
    Check if a specific bot runner process or swarm is alive.
    Uses psutil for fast lookup, falls back to risk state file existence.
    """
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline_list = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline_list)
                if "bot_runner.py" in cmdline_str and bot_name in cmdline_str:
                    return True
                if "run_swarm" in cmdline_str:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return data_path(f"{bot_name}_risk_state.json").exists()


def get_uptime() -> int:
    """Return seconds since swarm started (reads data/swarm_start_time.txt)."""
    start_file = data_path("swarm_start_time.txt")
    try:
        with open(start_file, "r") as fh:
            ts = float(fh.read().strip())
        return int(time.time() - ts)
    except Exception:
        return int(time.time() - START_TIME)


def today_utc_str() -> str:
    return date.today().strftime("%Y-%m-%d")


def llm_db_path() -> Path:
    return data_path("central_llm_controller.db")


def get_exchange_auth_status() -> Dict[str, Any]:
    """
    Determine if Kalshi exchange authentication and sensors are verified or degraded.
    Reads from latest health report, bot risk state, status files, and recent logs.
    """
    # 1. First check latest health report if fresh
    hr = read_json(data_path("health_report_latest.json"), {})
    kalshi_chk = hr.get("checks", {}).get("kalshi_auth", {})
    if kalshi_chk:
        status = str(kalshi_chk.get("status", "OK")).upper()
        if status in ("DEGRADED", "FAIL", "CRITICAL", "UNKNOWN"):
            return {
                "status": "degraded",
                "verified": False,
                "reason": kalshi_chk.get("details", "Exchange authentication degraded / offline"),
                "mode": kalshi_chk.get("mode", "DEMO"),
            }
        elif status == "OK" and kalshi_chk.get("authenticated") is True:
            return {
                "status": "ok",
                "verified": True,
                "reason": kalshi_chk.get("details", "Exchange authenticated and nominal"),
                "mode": kalshi_chk.get("mode", "DEMO"),
                "balance_cents": kalshi_chk.get("balance_cents", 0),
            }

    # 2. Check bot status and risk state files
    for bot in BOTS:
        st = read_status(bot)
        rs = read_risk_state(bot)
        if st.get("exchange_auth_status") == "degraded" or rs.get("exchange_auth_status") == "degraded":
            return {
                "status": "degraded",
                "verified": False,
                "reason": st.get("last_auth_error")
                or rs.get("last_auth_error")
                or "HTTP 401: Kalshi authentication failure",
                "mode": "DEMO",
            }
        if st.get("balance_verified") is False or rs.get("balance_verified") is False:
            return {
                "status": "degraded",
                "verified": False,
                "reason": "Exchange balance sensor unverified",
                "mode": "DEMO",
            }

    # 3. Check recent log lines for 401 / auth errors
    try:
        lp = log_path()
        if lp.exists():
            with open(lp, "rb") as fh:
                size = fh.seek(0, 2)
                fh.seek(max(0, size - 16384))
                recent = fh.read().decode("utf-8", errors="replace").splitlines()[-60:]
            for line in reversed(recent):
                if (
                    "Failed to fetch balance: HTTP 401" in line
                    or "Failed to fetch positions: HTTP 401" in line
                    or "401 Client Error: Unauthorized" in line
                    or "HTTP 401 AUTHENTICATION FAILURE" in line
                ):
                    return {
                        "status": "degraded",
                        "verified": False,
                        "reason": "HTTP 401 Unauthorized on Kalshi API",
                        "mode": "DEMO",
                    }
    except Exception:
        pass

    return {
        "status": "ok",
        "verified": True,
        "reason": "Exchange connection nominal",
        "mode": "DEMO",
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    """Serve the main dashboard HTML."""
    return render_template("index.html")


# ── /api/status ─────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """
    Portfolio-level overview: balances, daily PnL, trade counts, bot states.
    """
    bots_data = {}
    total_pnl = 0
    bot_balances = []
    exchange_auth = get_exchange_auth_status()
    auth_balance = exchange_auth.get("balance_cents")

    for bot in BOTS:
        risk = read_risk_state(bot)
        status = read_status(bot)

        bal = risk["balance_cents"]
        pnl = risk["daily_pnl_cents"]
        bot_balances.append(bal)
        total_pnl += pnl

        bot_verified = (
            exchange_auth["verified"]
            and risk.get("balance_verified", True)
            and status.get("balance_verified", True)
        )

        bots_data[bot] = {
            "balance_cents": bal,
            "balance_verified": bot_verified,
            "positions_verified": bot_verified and status.get("positions_verified", True),
            "daily_pnl_cents": pnl,
            "daily_trades": risk["daily_trades"],
            "max_trades": 8,
            "paused": is_paused(risk["pause_until"]),
            "active": bot_process_running(bot),
            "can_trade": status.get("can_trade", True) if exchange_auth["verified"] else False,
            "state": status.get("state", "unknown"),
        }

    # Unified Kalshi portfolio balance (not sum of duplicate bot readings)
    swarm_cfg = read_yaml(PROJECT_ROOT / "config" / "swarm_config.yaml") or {}
    demo_bankroll = int(swarm_cfg.get("trading", {}).get("demo_bankroll_cents", 0) or 0)
    is_demo = swarm_cfg.get("api", {}).get("demo_mode", True)

    if is_demo and demo_bankroll > 0:
        total_balance = max(demo_bankroll + total_pnl, max(bot_balances) if bot_balances else demo_bankroll)
    elif exchange_auth.get("verified") and auth_balance is not None:
        total_balance = int(auth_balance)
    elif bot_balances:
        total_balance = max(bot_balances)
    else:
        total_balance = 0

    # portfolio_change_pct = daily_pnl / (portfolio - daily_pnl) * 100
    base = total_balance - total_pnl
    change_pct = round((total_pnl / base * 100) if base else 0.0, 2)

    return json_response({
        "portfolio_cents": total_balance,
        "portfolio_change_pct": change_pct,
        "balance_verified": exchange_auth["verified"],
        "exchange_auth": exchange_auth,
        "bots": bots_data,
        "uptime_seconds": get_uptime(),
    })


# ── /api/llm ─────────────────────────────────────────────────────────────────

@app.route("/api/llm")
def api_llm():
    """
    LLM decision stats: today's totals, clean-period win-rate, recent decisions.
    """
    conn, err = open_db_readonly(llm_db_path())
    today = today_utc_str()

    # Defaults
    today_stats = {
        "total": 0, "approved": 0, "rejected": 0,
        "approval_rate_pct": None,
        "real_llm": 0, "quant_fallback": 0, "real_llm_pct": None,
        "per_bot": {b: {"total": 0, "approved": 0} for b in BOTS},
    }
    clean_period = {
        "total_resolved": 0, "wins": 0, "win_rate_pct": None, "start_date": today,
    }
    recent = []

    if conn is None:
        return json_response({"today": today_stats, "clean_period": clean_period,
                               "recent_decisions": recent, "error": err})

    try:
        # Check if table exists
        cur_t = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_decisions'")
        if not cur_t.fetchone():
            return json_response({"today": today_stats, "clean_period": clean_period, "recent_decisions": recent})

        # ── Today's decisions ─────────────────────────────────────────────
        cur = conn.execute(
            "SELECT bot_name, decision, rationale FROM llm_decisions "
            "WHERE date(timestamp) = date('now', 'utc')"
        )
        rows = cur.fetchall()
        per_bot = {b: {"total": 0, "approved": 0} for b in BOTS}
        total = approved = real_llm = 0

        for r in rows:
            total += 1
            bot_key = r["bot_name"] if r["bot_name"] in per_bot else None
            if bot_key:
                per_bot[bot_key]["total"] += 1
            if r["decision"] in ("approve", "approved"):
                approved += 1
                if bot_key:
                    per_bot[bot_key]["approved"] += 1
            rat = (r["rationale"] or "").lower()
            if "quant fallback" not in rat and "fail-closed" not in rat:
                real_llm += 1

        quant_fallback = total - real_llm
        today_stats = {
            "total":              total,
            "approved":           approved,
            "rejected":           total - approved,
            "approval_rate_pct":  round(approved / total * 100, 1) if total else None,
            "real_llm":           real_llm,
            "quant_fallback":     quant_fallback,
            "real_llm_pct":       round(real_llm / total * 100, 1) if total else None,
            "per_bot":            per_bot,
        }

        # ── Clean-period stats (all resolved outcomes) ────────────────────
        cur2 = conn.execute(
            "SELECT outcome, timestamp FROM llm_decisions "
            "WHERE outcome IS NOT NULL AND outcome != '' "
            "  AND (rationale NOT LIKE '%quant fallback%' "
            "   AND rationale NOT LIKE '%fail-closed%') "
            "ORDER BY id ASC"
        )
        resolved_rows = cur2.fetchall()
        wins = sum(1 for r in resolved_rows if r["outcome"] == "win")
        total_res = len(resolved_rows)
        start_date = resolved_rows[0]["timestamp"][:10] if resolved_rows else today
        clean_period = {
            "total_resolved":  total_res,
            "wins":            wins,
            "win_rate_pct":    round(wins / total_res * 100, 1) if total_res else None,
            "start_date":      start_date,
        }

        # ── Recent decisions (last 20) ────────────────────────────────────
        cur3 = conn.execute(
            "SELECT timestamp, bot_name, ticker, decision, llm_confidence, outcome, rationale "
            "FROM llm_decisions ORDER BY id DESC LIMIT 20"
        )
        recent = [
            {
                "timestamp":  r["timestamp"],
                "bot":        r["bot_name"],
                "ticker":     r["ticker"],
                "decision":   r["decision"],
                "confidence": r["llm_confidence"],
                "outcome":    r["outcome"],
                "rationale":  (r["rationale"] or "")[:120],
            }
            for r in cur3.fetchall()
        ]

    except sqlite3.OperationalError:
        pass
    except Exception as exc:
        return json_response({"today": today_stats, "clean_period": clean_period,
                               "recent_decisions": recent, "error": str(exc)})
    finally:
        conn.close()

    return json_response({
        "today":             today_stats,
        "clean_period":      clean_period,
        "recent_decisions":  recent,
    })


# ── /api/trades ──────────────────────────────────────────────────────────────

@app.route("/api/trades")
def api_trades():
    """
    Last 50 trades across all 4 bots, sorted by timestamp desc.
    Handles missing tables and column sets gracefully.
    """
    all_trades = []

    for bot in BOTS:
        db_file = data_path(f"{bot}.db")
        if not db_file.exists():
            continue
        conn, err = open_db_readonly(db_file)
        if conn is None:
            continue
        try:
            # Try to read with full column list first, fall back gracefully
            try:
                cur = conn.execute(
                    "SELECT ticker, side, outcome, pnl_cents, timestamp, confidence "
                    "FROM trades ORDER BY id DESC LIMIT 50"
                )
                for r in cur.fetchall():
                    all_trades.append({
                        "bot":        bot,
                        "ticker":     r["ticker"],
                        "side":       r["side"],
                        "outcome":    r["outcome"],
                        "pnl_cents":  r["pnl_cents"],
                        "timestamp":  r["timestamp"],
                        "confidence": r["confidence"],
                    })
            except sqlite3.OperationalError:
                # Columns may differ — fall back to just what's available
                cur_info = conn.execute("PRAGMA table_info(trades)")
                cols = {row["name"] for row in cur_info.fetchall()}
                select_cols = [c for c in
                               ["ticker", "side", "outcome", "pnl_cents", "timestamp", "confidence"]
                               if c in cols]
                if not select_cols:
                    continue
                cur = conn.execute(
                    f"SELECT {', '.join(select_cols)} FROM trades ORDER BY rowid DESC LIMIT 50"
                )
                for r in cur.fetchall():
                    trade = {"bot": bot}
                    for c in select_cols:
                        trade[c] = r[c]
                    all_trades.append(trade)
        except Exception:
            pass
        finally:
            conn.close()

    # Sort combined list by timestamp desc (ISO strings sort correctly)
    all_trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)
    return json_response(all_trades[:50])


# ── /api/supervisor ───────────────────────────────────────────────────────────

@app.route("/api/supervisor/latest")
def api_supervisor_latest():
    """Return the latest autonomous supervisor report."""
    report_file = data_path("supervisor_latest.json")
    if not report_file.exists():
        try:
            from swarm.llm_supervisor import LLMSupervisor
            sup = LLMSupervisor(project_root=PROJECT_ROOT)
            report = sup.run_audit()
            return json_response(report)
        except Exception as exc:
            return json_response({"ok": False, "error": str(exc)}, status=500)
    
    report = read_json(report_file, default={})
    return json_response(report)


@app.route("/api/supervisor/history")
def api_supervisor_history():
    """Return past supervisor audit runs from SQLite."""
    db_file = data_path("supervisor_reports.db")
    if not db_file.exists():
        return json_response([])
    conn, err = open_db_readonly(db_file)
    if conn is None:
        return json_response([])
    try:
        cur = conn.execute(
            "SELECT id, timestamp, health_grade, score_pct, total_findings, "
            "critical_count, warning_count, auto_actions_count, summary, findings_json, remediations_json "
            "FROM supervisor_audits ORDER BY id DESC LIMIT 20"
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "id": r["id"],
                "timestamp": r["timestamp"],
                "health_grade": r["health_grade"],
                "score_pct": r["score_pct"],
                "total_findings": r["total_findings"],
                "critical_count": r["critical_count"],
                "warning_count": r["warning_count"],
                "auto_actions_count": r["auto_actions_count"],
                "summary": r["summary"],
                "findings": json.loads(r["findings_json"]) if r["findings_json"] else [],
                "remediations": json.loads(r["remediations_json"]) if r["remediations_json"] else [],
            })
        return json_response(rows)
    except Exception as exc:
        return json_response({"error": str(exc)}, status=500)
    finally:
        conn.close()


@app.route("/api/supervisor/run_now", methods=["POST", "GET"])
def api_supervisor_run_now():
    """Trigger an immediate full audit pass by the LLM Supervisor."""
    try:
        from swarm.llm_supervisor import LLMSupervisor
        sup = LLMSupervisor(project_root=PROJECT_ROOT)
        report = sup.run_audit()
        return json_response({"ok": True, "report": report})
    except Exception as exc:
        return json_response({"ok": False, "error": str(exc)}, status=500)


# ── /api/positions ───────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    """
    Open position counts from risk_state and status files.
    """
    by_bot = {}
    total = 0
    exchange_auth = get_exchange_auth_status()

    for bot in BOTS:
        risk = read_risk_state(bot)
        status = read_status(bot)
        # Prefer status file's open_positions, fall back to risk_state
        count = status.get("open_positions") or risk.get("open_positions", 0)
        by_bot[bot] = count
        total += count

    return json_response({
        "total_open": total,
        "by_bot": by_bot,
        "positions_verified": exchange_auth["verified"],
        "exchange_auth": exchange_auth,
    })


# ── /api/equity ──────────────────────────────────────────────────────────────

@app.route("/api/equity")
def api_equity():
    """
    Chronological portfolio equity curve computed from settled trade outcomes.
    """
    all_settled = []
    exchange_auth = get_exchange_auth_status()
    if exchange_auth.get("verified") and exchange_auth.get("balance_cents") is not None:
        total_current_balance = int(exchange_auth["balance_cents"])
    else:
        bot_balances = [read_risk_state(b).get("balance_cents", 0) or 0 for b in BOTS]
        total_current_balance = max(bot_balances) if bot_balances else 0

    for bot in BOTS:
        db_file = data_path(f"{bot}.db")
        if not db_file.exists():
            continue
        conn, _ = open_db_readonly(db_file)
        if conn is None:
            continue
        try:
            cur = conn.execute(
                "SELECT timestamp, pnl_cents FROM trades "
                "WHERE outcome IN ('win', 'loss') AND pnl_cents IS NOT NULL "
                "ORDER BY timestamp ASC"
            )
            for r in cur.fetchall():
                ts = r["timestamp"]
                pnl = r["pnl_cents"] or 0
                if ts:
                    all_settled.append({"timestamp": ts, "pnl_cents": pnl})
        except Exception:
            pass
        finally:
            conn.close()

    # Sort chronologically
    all_settled.sort(key=lambda x: x["timestamp"])

    # Compute running equity curve
    cumulative = 0
    points = []
    for item in all_settled:
        cumulative += item["pnl_cents"]
        points.append({
            "timestamp": item["timestamp"],
            "cumulative_pnl_cents": cumulative,
            "portfolio_cents": total_current_balance + cumulative if total_current_balance > 0 else 10000 + cumulative,
        })

    # If no settled trades yet, return a baseline starting point
    if not points:
        points.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portfolio_cents": total_current_balance or 10000,
        })

    # Always ensure current real-time point is at the end
    now_iso = datetime.now(timezone.utc).isoformat()
    if not points or points[-1]["timestamp"] != now_iso:
        points.append({
            "timestamp": now_iso,
            "portfolio_cents": total_current_balance if total_current_balance > 0 else (points[-1]["portfolio_cents"] if points else 10000),
        })

    return json_response(points)


# ── /api/risk ────────────────────────────────────────────────────────────────

@app.route("/api/risk")
def api_risk():
    """
    Per-bot risk state and guardrail progress toward loosening thresholds.
    """
    bots_risk = {}
    exchange_auth = get_exchange_auth_status()
    for bot in BOTS:
        risk = read_risk_state(bot)
        status = read_status(bot)

        bal = risk.get("balance_cents", 0) or 0
        peak = risk.get("peak_balance_cents", 0) or 0
        dd = round((peak - bal) / peak * 100, 2) if peak and peak > 0 else None

        bot_verified = (
            exchange_auth["verified"]
            and risk.get("balance_verified", True)
            and status.get("balance_verified", True)
        )

        bots_risk[bot] = {
            "balance_cents": bal,
            "balance_verified": bot_verified,
            "positions_verified": bot_verified and status.get("positions_verified", True),
            "peak_balance_cents": peak,
            "drawdown_pct": dd,
            "daily_pnl_cents": risk["daily_pnl_cents"],
            "daily_trades": risk["daily_trades"],
            "can_trade": status.get("can_trade", True),
            "paused": is_paused(risk["pause_until"]),
        }

    # ── Guardrail progress (from LLM clean period) ────────────────────────
    guardrail = {
        "win_rate_current":    0.0,
        "win_rate_target":     55.0,
        "trade_count_current": 0,
        "trade_count_target":  50,
        "days_positive_pnl":   0,
        "days_positive_target": 14,
        "ready_to_loosen":     False,
    }
    conn, _ = open_db_readonly(llm_db_path())
    if conn:
        try:
            cur = conn.execute(
                "SELECT outcome FROM llm_decisions "
                "WHERE outcome IS NOT NULL AND outcome != '' "
                "  AND rationale NOT LIKE '%quant fallback%' "
                "  AND rationale NOT LIKE '%fail-closed%'"
            )
            resolved = cur.fetchall()
            total_res = len(resolved)
            wins = sum(1 for r in resolved if r["outcome"] == "win")
            win_rate = round(wins / total_res * 100, 1) if total_res else 0.0

            # Days with positive daily PnL — use daily_summary if available
            days_pos = 0
            try:
                cur2 = conn.execute(
                    "SELECT COUNT(DISTINCT date(timestamp)) as d FROM llm_decisions "
                    "WHERE pnl_cents > 0"
                )
                days_pos = cur2.fetchone()["d"] or 0
            except Exception:
                pass

            guardrail.update({
                "win_rate_current":    win_rate,
                "trade_count_current": total_res,
                "days_positive_pnl":   days_pos,
                "ready_to_loosen": (
                    win_rate >= 55.0
                    and total_res >= 50
                    and days_pos >= 14
                ),
            })
        except Exception:
            pass
        finally:
            conn.close()

    return json_response({
        "bots": bots_risk,
        "guardrail_progress": guardrail,
        "exchange_auth": exchange_auth,
        "balance_verified": exchange_auth["verified"],
    })


# ── /api/learning ────────────────────────────────────────────────────────────

@app.route("/api/learning")
def api_learning():
    """
    Demo trading learning & adaptation analytics:
    Confidence calibration, feature weights history, category multipliers, and LLM filtering.
    """
    try:
        from watch_demo_learning import aggregate_full_learning_report
        report = aggregate_full_learning_report(data_path())
        return json_response(report)
    except Exception as exc:
        saved = read_json(data_path("learning_report_latest.json"))
        if saved:
            return json_response(saved)
        return json_response({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scorecard": {
                "is_learning": "INSUFFICIENT_DATA",
                "stage": "WARMUP",
                "status_message": "Warmup phase — gathering baseline data.",
                "evidence": ["Total Recorded Trades: 0 across 4 bots."],
            },
            "calibration": {
                "expected_calibration_error": None,
                "brier_score": None,
                "brier_score_prior": 0.2500,
                "calibration_bias": None,
                "calibration_verdict": "Insufficient Data (Awaiting settled trades)",
                "buckets": [],
            },
            "categories": {
                "overall_win_rate_pct": None,
                "hot_categories": [],
                "cold_categories": [],
                "categories": [],
            },
            "weights": {"total_recalibrations": 0, "latest_weights_by_bot": {}},
            "llm_intelligence": {
                "total_decisions": 0,
                "approved": 0,
                "rejected": 0,
                "approval_rate_pct": None,
                "top_red_flags": [],
            },
            "rolling_win_rates": [],
        })


# ── /api/system ──────────────────────────────────────────────────────────────

@app.route("/api/system")
def api_system():
    """
    System health: Tavily usage, Anthropic status, uptime, log tail, health report.
    """
    # ── Log tail (last 10 lines) ──────────────────────────────────────────
    log_lines = []
    try:
        lp = log_path()
        if lp.exists():
            with open(lp, "rb") as fh:
                size = fh.seek(0, 2)
                fh.seek(max(0, size - 8192))
                lines = [l.strip() for l in fh.read().decode("utf-8", errors="replace").splitlines() if l.strip()]
                log_lines = lines[-10:]
        else:
            log_lines = ["Log file not found"]
    except Exception as exc:
        log_lines = [f"Error reading log: {exc}"]

    # ── Tavily usage (best-effort tail check) ─────────────────────────────
    tavily_today = 0
    try:
        today_prefix = today_utc_str()
        lp = log_path()
        if lp.exists():
            with open(lp, "rb") as fh:
                size = fh.seek(0, 2)
                fh.seek(max(0, size - 65536))
                for line in fh.read().decode("utf-8", errors="replace").splitlines():
                    if today_prefix not in line:
                        continue
                    ll = line.lower()
                    if "tavily" in ll and "exhausted" not in ll and "budget" not in ll and "error" not in ll:
                        tavily_today += 1
    except Exception:
        pass

    tavily_budget = 30
    tavily_pct = round(tavily_today / tavily_budget * 100, 1) if tavily_budget else 0.0

    # ── Active LLM Brain status ───────────────────────────────────────────
    cfg_file = config_path("swarm_config.yaml")
    llm_provider = "gemini"
    llm_model = "gemini-2.5-flash"
    try:
        with open(cfg_file, "r", encoding="utf-8") as fh:
            c = yaml.safe_load(fh) or {}
            central = c.get("central_llm", {})
            llm_provider = str(central.get("provider", "gemini")).lower()
            if llm_provider == "gemini":
                llm_model = central.get("gemini_model", "gemini-2.5-flash")
            elif llm_provider in {"anthropic", "claude"}:
                llm_model = central.get("anthropic_model", "claude-3-5-haiku-latest")
            else:
                llm_model = central.get("model", "qwen2.5:14b")
    except Exception:
        pass

    llm_status = "ok"
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or central.get("gemini_api_key")
    if llm_provider == "gemini" and not gemini_key:
        llm_status = "error"
    else:
        conn, _ = open_db_readonly(llm_db_path())
        if conn:
            try:
                cur = conn.execute(
                    "SELECT rationale FROM llm_decisions ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row and row["rationale"] and ("401" in row["rationale"] or "LLM failed" in row["rationale"]):
                    llm_status = "error"
            except Exception:
                pass
            finally:
                conn.close()

    # ── Health report ─────────────────────────────────────────────────────
    health_report = read_json(data_path("health_report_latest.json"), {})

    return json_response({
        "tavily": {
            "used_today": tavily_today,
            "budget":     tavily_budget,
            "pct":        tavily_pct,
        },
        "llm_provider":     llm_provider,
        "llm_model":        llm_model,
        "llm_status":       llm_status,
        "anthropic_status": llm_status,
        "exchange_auth":    get_exchange_auth_status(),
        "uptime_seconds":   get_uptime(),
        "log_tail":         log_lines,
        "health_report":    health_report,
    })


# ── /api/config ──────────────────────────────────────────────────────────────

@app.route("/api/config")
def api_config():
    """Return swarm_config.yaml as both parsed JSON and raw YAML string."""
    cfg_file = config_path("swarm_config.yaml")
    try:
        with open(cfg_file, "r", encoding="utf-8") as fh:
            raw_yaml = fh.read()
        parsed = yaml.safe_load(raw_yaml) or {}
    except FileNotFoundError:
        return json_response({"error": "swarm_config.yaml not found", "raw": "", "parsed": {}}, 404)
    except Exception as exc:
        return json_response({"error": str(exc), "raw": "", "parsed": {}}, 500)

    return json_response({"parsed": parsed, "raw": raw_yaml})


# ── /api/health ──────────────────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    """Return the latest health report JSON."""
    report = read_json(data_path("health_report_latest.json"), {})
    if not report:
        return json_response({"error": "health_report_latest.json not found or empty"}, 404)
    return json_response(report)





# ── /api/control/pause/<bot> ─────────────────────────────────────────────────

@app.route("/api/control/pause/<bot_name>", methods=["POST", "OPTIONS"])
def api_pause(bot_name: str):
    """Write a pause signal file for the named bot."""
    if request.method == "OPTIONS":
        return json_response({})
    if bot_name not in BOTS:
        return json_response({"ok": False, "error": f"Unknown bot: {bot_name}"}, 400)
    signal = {"command": "pause", "action": "pause", "timestamp": datetime.now(timezone.utc).isoformat()}
    write_json(data_path(f"{bot_name}_signal.json"), signal)
    write_json(data_path(f"{bot_name}_pause_signal.json"), signal)
    return json_response({"ok": True, "bot": bot_name, "action": "pause"})


# ── /api/control/resume/<bot> ────────────────────────────────────────────────

@app.route("/api/control/resume/<bot_name>", methods=["POST", "OPTIONS"])
def api_resume(bot_name: str):
    """Write a resume signal file for the named bot."""
    if request.method == "OPTIONS":
        return json_response({})
    if bot_name not in BOTS:
        return json_response({"ok": False, "error": f"Unknown bot: {bot_name}"}, 400)
    signal = {"command": "resume", "action": "resume", "timestamp": datetime.now(timezone.utc).isoformat()}
    write_json(data_path(f"{bot_name}_signal.json"), signal)
    write_json(data_path(f"{bot_name}_pause_signal.json"), signal)
    return json_response({"ok": True, "bot": bot_name, "action": "resume"})


# ── /api/admin/logs ──────────────────────────────────────────────────────────

@app.route("/api/admin/logs", methods=["POST", "OPTIONS"])
def api_admin_logs():
    """Return last N lines of swarm.log (body: {"lines": 50})."""
    if request.method == "OPTIONS":
        return json_response({})
    body = request.get_json(silent=True) or {}
    n = int(body.get("lines", 50))
    n = max(1, min(n, 1000))  # clamp to [1, 1000]

    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        lines = [l.rstrip() for l in all_lines[-n:]]
    except FileNotFoundError:
        return json_response({"ok": False, "error": "Log file not found", "lines": []}, 404)
    except Exception as exc:
        return json_response({"ok": False, "error": str(exc), "lines": []}, 500)

    return json_response({"ok": True, "count": len(lines), "lines": lines})


# ── /api/admin/vacuum ────────────────────────────────────────────────────────

@app.route("/api/admin/vacuum", methods=["POST", "OPTIONS"])
def api_admin_vacuum():
    """Run VACUUM on every .db file in the data/ directory."""
    if request.method == "OPTIONS":
        return json_response({})
    results = {}
    data_dir = PROJECT_ROOT / "data"

    for db_file in sorted(data_dir.glob("*.db")):
        try:
            conn = sqlite3.connect(str(db_file), timeout=10)
            conn.execute("VACUUM")
            conn.close()
            results[db_file.name] = "ok"
        except Exception as exc:
            results[db_file.name] = f"error: {exc}"

    return json_response({"ok": True, "vacuumed": results})


# ── /api/config/save ─────────────────────────────────────────────────────────

@app.route("/api/config/save", methods=["POST", "OPTIONS"])
def api_config_save():
    """
    Accept raw YAML body, backup current config, write new config.
    Body should be raw YAML text (Content-Type: text/plain or application/json with 'yaml' key).
    """
    if request.method == "OPTIONS":
        return json_response({})

    # Accept either plain text body or {"yaml": "..."} JSON
    if request.content_type and "application/json" in request.content_type:
        body = request.get_json(silent=True) or {}
        raw_yaml = body.get("yaml", "")
    else:
        raw_yaml = request.get_data(as_text=True)

    if not raw_yaml.strip():
        return json_response({"ok": False, "error": "Empty YAML body"}, 400)

    # Validate YAML before saving
    try:
        yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        return json_response({"ok": False, "error": f"Invalid YAML: {exc}"}, 400)

    cfg_file = config_path("swarm_config.yaml")
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    backup_file = config_path(f"swarm_config.yaml.bak.{ts}")

    try:
        # Backup existing config if present
        if cfg_file.exists():
            import shutil
            shutil.copy2(cfg_file, backup_file)

        with open(cfg_file, "w", encoding="utf-8") as fh:
            fh.write(raw_yaml)
    except Exception as exc:
        return json_response({"ok": False, "error": str(exc)}, 500)

    return json_response({"ok": True, "backup": str(backup_file)})


# ── Swarm Command Console Endpoints ─────────────────────────────────────────

def _get_swarm_processes():
    """Locate all running swarm processes using psutil with fallback."""
    found = []
    target_scripts = (
        "run_swarm.py",
        "run_swarm_with_brain.py",
        "run_swarm_with_ollama_brain.py",
        "swarm_daemon.py",
        "bot_runner.py",
    )
    # 1. Try psutil (instant, non-blocking, cross-platform)
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                if proc.pid == os.getpid():
                    continue
                cmdline_list = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline_list)
                if any(t in cmdline_str for t in target_scripts):
                    found.append({"pid": proc.pid, "cmd": cmdline_str})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return found
    except Exception:
        pass

    # 2. Fallback if psutil is unavailable
    import subprocess
    if os.name == "nt":
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' } | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.stdout.strip():
                data = json.loads(res.stdout.strip())
                if isinstance(data, dict):
                    data = [data]
                for proc in data:
                    cmdline = proc.get("CommandLine") or ""
                    pid = proc.get("ProcessId")
                    if any(t in cmdline for t in target_scripts) and pid != os.getpid():
                        found.append({"pid": pid, "cmd": cmdline})
        except Exception:
            pass
    else:
        try:
            res = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if any(t in line for t in target_scripts) and "grep" not in line and str(os.getpid()) not in line:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        found.append({"pid": int(parts[1]), "cmd": line})
        except Exception:
            pass
    return found


@app.route("/api/swarm/status")
def api_swarm_status():
    """Live swarm process status, trading mode, and AI brain model."""
    procs = _get_swarm_processes()
    cfg_file = config_path("swarm_config.yaml")
    is_demo = True
    provider = "gemini"
    model = "gemini-2.5-flash"
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            c = yaml.safe_load(f) or {}
            is_demo = bool(c.get("api", {}).get("demo_mode", True))
            central = c.get("central_llm", {})
            provider = central.get("provider", "gemini")
            model = central.get("gemini_model", "gemini-2.5-flash")
    except Exception:
        pass

    return json_response({
        "running": len(procs) > 0,
        "process_count": len(procs),
        "processes": procs,
        "mode": "demo" if is_demo else "live",
        "demo_mode": is_demo,
        "provider": provider,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/api/swarm/start", methods=["POST", "OPTIONS"])
def api_swarm_start():
    """Start the swarm in the background."""
    if request.method == "OPTIONS":
        return json_response({})

    procs = _get_swarm_processes()
    if procs:
        msg = f"Swarm already running ({len(procs)} process(es) active)."
        return json_response({
            "ok": True,
            "message": msg,
            "output": msg,
            "pids": [p["pid"] for p in procs]
        })

    try:
        import subprocess
        run_script = PROJECT_ROOT / "run_swarm_with_brain.py"
        if not run_script.exists():
            run_script = PROJECT_ROOT / "run_swarm.py"

        logs_dir = PROJECT_ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = (logs_dir / "swarm.log").open("a", encoding="utf-8")

        if os.name == "nt":
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            proc = subprocess.Popen(
                [sys.executable, str(run_script)],
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=log_file,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        else:
            proc = subprocess.Popen(
                [sys.executable, str(run_script)],
                cwd=str(PROJECT_ROOT),
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                close_fds=True,
            )

        time.sleep(1.0)
        new_procs = _get_swarm_processes()
        msg = f"Swarm started successfully (PID: {proc.pid})."
        return json_response({
            "ok": True,
            "message": msg,
            "output": msg,
            "pid": proc.pid,
            "active_processes": new_procs,
        })
    except Exception as exc:
        return json_response({
            "ok": False,
            "error": str(exc),
            "output": f"Failed to start swarm: {exc}",
        }, 500)


@app.route("/api/swarm/stop", methods=["POST", "OPTIONS"])
def api_swarm_stop():
    """Stop all running swarm processes."""
    if request.method == "OPTIONS":
        return json_response({})

    procs = _get_swarm_processes()
    if not procs:
        msg = "No active swarm processes found."
        return json_response({"ok": True, "message": msg, "output": msg})

    try:
        import subprocess, signal
        # Send kill signal file
        write_json(data_path("kill_signal.json"), {
            "action": "kill",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "requested_by": "command_console",
        })

        killed = []
        for p in procs:
            pid = p["pid"]
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                else:
                    os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except Exception:
                pass

        time.sleep(0.5)
        msg = f"Stopped {len(killed)} swarm process(es)."
        return json_response({
            "ok": True,
            "message": msg,
            "output": msg,
            "stopped_pids": killed,
        })
    except Exception as exc:
        return json_response({"ok": False, "error": str(exc), "output": f"Failed to stop swarm: {exc}"}, 500)


@app.route("/api/swarm/restart", methods=["POST", "OPTIONS"])
def api_swarm_restart():
    """Restart the swarm."""
    if request.method == "OPTIONS":
        return json_response({})
    api_swarm_stop()
    time.sleep(1.0)
    return api_swarm_start()


def _set_trading_mode(new_mode: str):
    """Internal helper to switch trading mode in config."""
    new_mode = str(new_mode).strip().lower()
    if new_mode not in ("demo", "live"):
        return json_response({"ok": False, "error": "Mode must be 'demo' or 'live'"}, 400)

    cfg_file = config_path("swarm_config.yaml")
    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if "api" not in cfg or not isinstance(cfg["api"], dict):
            cfg["api"] = {}

        is_demo = (new_mode == "demo")
        cfg["api"]["demo_mode"] = is_demo

        with open(cfg_file, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

        procs = _get_swarm_processes()
        restart_needed = len(procs) > 0

        msg = f"Trading mode switched to {new_mode.upper()}."
        if restart_needed:
            msg += " (Restart swarm for changes to take effect)"

        return json_response({
            "ok": True,
            "mode": new_mode,
            "demo_mode": is_demo,
            "message": msg,
            "restart_needed": restart_needed,
        })
    except Exception as exc:
        return json_response({"ok": False, "error": str(exc)}, 500)


@app.route("/api/swarm/mode", methods=["POST", "OPTIONS"])
def api_swarm_mode():
    """Switch trading mode to 'demo' or 'live'."""
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    new_mode = str(body.get("mode", "")).strip().lower()
    return _set_trading_mode(new_mode)


@app.route("/api/swarm/exec", methods=["POST", "OPTIONS"])
@app.route("/api/swarm/command", methods=["POST", "OPTIONS"])
def api_swarm_exec():
    """Execute console commands and return terminal output."""
    if request.method == "OPTIONS":
        return json_response({})

    body = request.get_json(silent=True) or {}
    cmd_str = str(body.get("command", "")).strip()
    if not cmd_str:
        return json_response({"ok": False, "error": "Empty command"}, 400)

    import subprocess
    parts = cmd_str.split()
    verb = parts[0].lower().lstrip("/")

    if verb in ("help", "?", "commands"):
        help_text = (
            "Available Swarm Console Commands:\n"
            "  • start          - Launch background swarm processes with Gemini Central LLM\n"
            "  • stop           - Gracefully halt all bot workers and swarm daemon\n"
            "  • restart        - Restart the swarm session\n"
            "  • status / ps    - Display live execution state, process PIDs, and bot P&L\n"
            "  • mode demo      - Switch to safe simulated orders (Kalshi Demo API)\n"
            "  • mode live      - Switch to live capital execution (Real funds)\n"
            "  • health / check - Execute automated 15-point system verification\n"
            "  • radar / eval   - Run demo learning & calibration audit\n"
            "  • vacuum         - Reclaim unallocated disk space across SQLite databases\n"
            "  • logs [N]       - Print the last N lines (default 25) of swarm.log\n"
            "  • clear          - Clear terminal window"
        )
        return json_response({"ok": True, "output": help_text})
    elif verb in ("start", "launch"):
        return api_swarm_start()
    elif verb in ("stop", "kill", "halt"):
        return api_swarm_stop()
    elif verb in ("restart", "reboot"):
        return api_swarm_restart()
    elif verb == "mode":
        if len(parts) > 1:
            return _set_trading_mode(parts[1])
        else:
            procs = _get_swarm_processes()
            cfg_file = config_path("swarm_config.yaml")
            is_demo = True
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    c = yaml.safe_load(f) or {}
                    is_demo = bool(c.get("api", {}).get("demo_mode", True))
            except Exception:
                pass
            current = "DEMO (Simulation)" if is_demo else "LIVE CAPITAL"
            return json_response({
                "ok": True,
                "output": f"Current Trading Mode: {current}\nUsage to switch: mode demo  OR  mode live"
            })
    elif verb in ("status", "ps"):
        procs = _get_swarm_processes()
        cfg_file = config_path("swarm_config.yaml")
        is_demo = True
        provider = "gemini"
        model = "gemini-2.5-flash"
        search_ground = True
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                c = yaml.safe_load(f) or {}
                is_demo = bool(c.get("api", {}).get("demo_mode", True))
                central = c.get("central_llm", {})
                provider = central.get("provider", "gemini")
                model = central.get("gemini_model", "gemini-2.5-flash")
                search_ground = bool(central.get("gemini_search_grounding", True))
        except Exception:
            pass

        lines = [
            "============================================================",
            "           KALSHI SWARM COMMAND CONSOLE STATUS           ",
            "============================================================",
            f" Swarm Status   : {'🟢 RUNNING' if procs else '🔴 STOPPED'}",
            f" Trading Mode   : {'🟡 DEMO (Simulation)' if is_demo else '🔴 LIVE CAPITAL'}",
            f" AI Brain       : 🔷 {provider.upper()} ({model})",
            f" Search Ground  : {'✅ Google Search Enabled' if search_ground else '❌ Off'}",
            f" Config File    : {cfg_file}",
            "------------------------------------------------------------",
        ]
        if procs:
            lines.append(" Active Processes:")
            for p in procs:
                lines.append(f"   • PID {p['pid']:<7}")
        else:
            lines.append(" Active Processes: None")
        lines.append("------------------------------------------------------------")
        lines.append(" Bot Statuses:")
        for bot in BOTS:
            risk = read_risk_state(bot)
            pnl_cents = risk.get("daily_pnl_cents", 0) or 0
            pnl_str = f"{pnl_cents/100:+.2f}$"
            trades = risk.get("daily_trades", 0)
            paused = is_paused(risk.get("pause_until"))
            st = "PAUSED" if paused else ("RUNNING" if procs else "IDLE")
            lines.append(f"   • {bot:<10}: {st:<10} | Daily PnL: {pnl_str:<9} | Trades: {trades}/8")
        lines.append("============================================================")
        return json_response({"ok": True, "output": "\n".join(lines)})
    elif verb in ("health", "check"):
        try:
            res = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "health_check.py")],
                capture_output=True, text=True, timeout=20, cwd=str(PROJECT_ROOT)
            )
            out = (res.stdout + "\n" + res.stderr).strip()
            return json_response({"ok": True, "output": out})
        except Exception as e:
            return json_response({"ok": False, "error": str(e)})
    elif verb in ("radar", "learning", "eval"):
        try:
            res = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "watch_demo_learning.py"), "--save-report"],
                capture_output=True, text=True, timeout=20, cwd=str(PROJECT_ROOT)
            )
            out = (res.stdout + "\n" + res.stderr).strip()
            return json_response({"ok": True, "output": out})
        except Exception as e:
            return json_response({"ok": False, "error": str(e)})
    elif verb == "vacuum":
        results = {}
        data_dir = PROJECT_ROOT / "data"
        for db_file in sorted(data_dir.glob("*.db")):
            try:
                conn = sqlite3.connect(str(db_file), timeout=10)
                conn.execute("VACUUM")
                conn.close()
                results[db_file.name] = "VACUUM OK"
            except Exception as exc:
                results[db_file.name] = f"ERROR: {exc}"
        out = "\n".join([f"  • {k:<25}: {v}" for k, v in results.items()])
        return json_response({"ok": True, "output": f"Database VACUUM Routine:\n{out}"})
    elif verb == "logs":
        n = 25
        if len(parts) > 1:
            try:
                n = int(parts[1])
            except ValueError:
                n = 25
        n = max(1, min(n, 200))
        try:
            with open(log_path(), "r", encoding="utf-8", errors="replace") as fh:
                all_lines = fh.readlines()
            lines = [l.rstrip() for l in all_lines[-n:]]
            return json_response({"ok": True, "output": f"--- Last {len(lines)} lines of swarm.log ---\n" + "\n".join(lines)})
        except Exception as exc:
            return json_response({"ok": False, "error": str(exc)})
    elif verb in ("kill", "emergency_kill"):
        report = verify_and_cancel_exchange_orders()
        procs = _get_swarm_processes()
        killed = []
        for p in procs:
            try:
                os.kill(p["pid"], 9)
                killed.append(p["pid"])
            except Exception:
                pass
        return json_response({
            "ok": True,
            "output": (
                f"[EMERGENCY KILL EXECUTED]\n"
                f"  • Processes Terminated : {len(killed)} ({killed})\n"
                f"  • Exchange Status      : {report.get('status')}\n"
                f"  • Exchange Verified    : {'✅ YES' if report.get('exchange_verified') else '❌ UNVERIFIED'}\n"
                f"  • Details              : {report.get('details')}"
            )
        })
    elif verb in ("cancel", "cancel_all"):
        report = verify_and_cancel_exchange_orders()
        return json_response({
            "ok": report.get("ok", False),
            "output": (
                f"[EXCHANGE ORDER CANCELLATION]\n"
                f"  • Status            : {report.get('status')}\n"
                f"  • Exchange Verified : {'✅ YES' if report.get('exchange_verified') else '❌ UNVERIFIED'}\n"
                f"  • Orders Found      : {report.get('orders_found', 0)}\n"
                f"  • Orders Cancelled  : {report.get('orders_cancelled', 0)}\n"
                f"  • Remaining Resting : {report.get('remaining_orders', 0)}\n"
                f"  • Details           : {report.get('details')}"
            )
        })
    elif verb in ("supervisor", "audit", "overseer"):
        try:
            from swarm.llm_supervisor import LLMSupervisor
            sup = LLMSupervisor(project_root=PROJECT_ROOT)
            report = sup.run_audit()
            findings_summary = "\n".join([f"  • [{f.get('severity', 'info').upper()}] {f.get('title')}: {f.get('description')}" for f in report.get("findings", [])]) or "  • No issues found."
            remediations_summary = "\n".join([f"  • {r.get('type')}: {r.get('category', r.get('bot_name'))}" for r in report.get("auto_remediations", [])]) or "  • None required."
            ai_synthesis_text = f"\nAI Executive Synthesis:\n{report.get('ai_synthesis')}\n" if report.get('ai_synthesis') else ""
            out = (
                f"============================================================\n"
                f"       AUTONOMOUS LLM SUPERVISOR & META-AUDITOR             \n"
                f"============================================================\n"
                f" Health Grade     : {report.get('health_grade')} ({report.get('score_pct')}%)\n"
                f" Timestamp        : {report.get('timestamp')}\n"
                f" Total Findings   : {report.get('total_findings')} (Critical: {report.get('critical_count')}, Warning: {report.get('warning_count')})\n"
                f" Auto-Remediations: {report.get('auto_actions_count')} executed\n"
                f"------------------------------------------------------------\n"
                f"Findings:\n{findings_summary}\n"
                f"------------------------------------------------------------\n"
                f"Auto-Remediations Executed:\n{remediations_summary}"
                f"{ai_synthesis_text}\n"
                f"============================================================"
            )
            return json_response({"ok": True, "output": out})
        except Exception as e:
            return json_response({"ok": False, "error": str(e)})
    else:
        return json_response({
            "ok": False,
            "error": f"Unknown command '{cmd_str}'. Type 'help' for available commands (start, stop, restart, mode, status, health, radar, supervisor, vacuum, logs, cancel, kill, clear)."
        }, 400)


def verify_and_cancel_exchange_orders() -> Dict[str, Any]:
    """
    Query resting orders on Kalshi exchange, cancel them, and verify that
    the resting orders list is empty. Returns verified outcome diagnostics.
    """
    cfg = load_yaml("swarm_config.yaml") or {}
    api_cfg = cfg.get("api", {}) if isinstance(cfg, dict) else {}
    key_id = api_cfg.get("key_id") or os.environ.get("KALSHI_API_KEY_ID")
    pk_path = api_cfg.get("private_key_path", "keys/kalshi-private.key")
    demo_mode = bool(api_cfg.get("demo_mode", True))
    base_url = api_cfg.get("base_url") or (
        "https://demo-api.kalshi.co/trade-api/v2" if demo_mode else "https://api.elections.kalshi.com/trade-api/v2"
    )
    key_file = PROJECT_ROOT / pk_path if not Path(pk_path).is_absolute() else Path(pk_path)

    if not key_id or not key_file.exists():
        return {
            "ok": False,
            "status": "UNVERIFIED_AUTH_FAILURE",
            "exchange_verified": False,
            "orders_found": 0,
            "orders_cancelled": 0,
            "remaining_orders": 0,
            "details": "Kalshi API credentials missing (key_id or private key file not found). Cannot query or cancel orders on exchange.",
        }

    try:
        from kalshi_agent.kalshi_client import KalshiClient
        client = KalshiClient(
            api_key_id=key_id,
            private_key_path=str(key_file),
            base_url=base_url,
            demo_mode=demo_mode,
        )
        orders = client.get_orders(status="resting")
        orders_found = len(orders)
        if orders_found == 0:
            return {
                "ok": True,
                "status": "VERIFIED_CLEARED",
                "exchange_verified": True,
                "orders_found": 0,
                "orders_cancelled": 0,
                "remaining_orders": 0,
                "details": "Verified 0 resting orders on Kalshi exchange (exchange clean).",
            }

        cancelled = 0
        for o in orders:
            oid = o.get("order_id") or o.get("id")
            if oid:
                try:
                    client.cancel_order(oid)
                    cancelled += 1
                except Exception:
                    pass

        # Verification pass: confirm orders are gone
        remaining = client.get_orders(status="resting")
        remaining_cnt = len(remaining)
        if remaining_cnt == 0:
            return {
                "ok": True,
                "status": "VERIFIED_CLEARED",
                "exchange_verified": True,
                "orders_found": orders_found,
                "orders_cancelled": cancelled,
                "remaining_orders": 0,
                "details": f"Successfully cancelled {cancelled}/{orders_found} orders. Verified 0 resting orders remaining on exchange.",
            }
        else:
            return {
                "ok": False,
                "status": "PARTIAL_FAILURE",
                "exchange_verified": True,
                "orders_found": orders_found,
                "orders_cancelled": cancelled,
                "remaining_orders": remaining_cnt,
                "details": f"Cancelled {cancelled}/{orders_found} orders, but {remaining_cnt} resting orders remain on exchange.",
            }
    except Exception as exc:
        err_str = str(exc)
        is_auth = "401" in err_str or "unauthorized" in err_str.lower()
        return {
            "ok": False,
            "status": "UNVERIFIED_AUTH_FAILURE" if is_auth else "UNVERIFIED_NETWORK_FAILURE",
            "exchange_verified": False,
            "orders_found": 0,
            "orders_cancelled": 0,
            "remaining_orders": 0,
            "details": f"Exchange cancellation UNVERIFIED due to API error ({err_str[:90]}).",
            "error": err_str,
        }


@app.route("/api/kill", methods=["POST"])
def api_kill():
    """Emergency master kill switch: cancels exchange orders, halts processes, triggers flag."""
    body = request.get_json(silent=True) or {}
    if body.get("confirm", "").strip().upper() != "KILL":
        return json_response({"ok": False, "error": "Type KILL to confirm."}, 400)

    # 1. Verify and cancel exchange orders
    cancellation_report = verify_and_cancel_exchange_orders()

    # 2. Terminate local swarm processes
    procs = _get_swarm_processes()
    killed_pids = []
    for p in procs:
        try:
            os.kill(p["pid"], 9)
            killed_pids.append(p["pid"])
        except Exception:
            pass

    # 3. Create master kill trigger file
    kill_flag = PROJECT_ROOT / "data" / "emergency_kill.trigger"
    try:
        kill_flag.write_text(f"TRIGGERED at {datetime.now(timezone.utc).isoformat()}", encoding="utf-8")
    except Exception:
        pass

    return json_response({
        "ok": True,
        "killed_pids": killed_pids,
        "cancellation": cancellation_report,
        "message": f"Kill signal executed ({len(killed_pids)} processes terminated). Exchange: {cancellation_report['status']} — {cancellation_report['details']}",
    })


@app.route("/api/emergency/cancel_orders", methods=["POST"])
def api_emergency_cancel_orders():
    """Explicit endpoint to cancel and verify all resting orders on the exchange."""
    report = verify_and_cancel_exchange_orders()
    return json_response(report)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Kalshi Swarm Dashboard Server")
    parser.add_argument("--port",         type=int, default=8888,
                        help="Port to listen on (default: 8888)")
    parser.add_argument("--host",         type=str, default="0.0.0.0",
                        help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--project-root", type=str,
                        default=str(Path(__file__).parent.parent),
                        help="Path to Swarm-Kalshi project root")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Set module-level globals used by route handlers
    PROJECT_ROOT = Path(args.project_root).resolve()
    _load_env_file()

    # Ensure log directory exists and safely redirect if no console
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or (hasattr(sys.stdout, "isatty") and not sys.stdout.isatty()):
        try:
            log_fh = open(logs_dir / "dashboard.log", "a", encoding="utf-8", buffering=1)
            sys.stdout = log_fh
            sys.stderr = log_fh
        except Exception:
            pass

    print(f"[dashboard] Starting on http://{args.host}:{args.port}")
    print(f"[dashboard] Project root: {PROJECT_ROOT}")
    print(f"[dashboard] Data dir:     {PROJECT_ROOT / 'data'}")

    app.run(host=args.host, port=args.port, debug=False)
