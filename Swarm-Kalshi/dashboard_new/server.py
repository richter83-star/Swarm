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
import time
import traceback
from datetime import datetime, timezone, date
from functools import wraps
from pathlib import Path

import yaml
from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

app = Flask(__name__, template_folder="templates", static_folder="static")

# Set at startup via argparse
PROJECT_ROOT: Path = Path(__file__).parent.parent
START_TIME: float = time.time()

BOTS = ["sentinel", "oracle", "pulse", "vanguard"]

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


def open_db_readonly(db_path: Path):
    """
    Open a SQLite database in WAL mode with a short timeout.
    Returns (conn, None) on success or (None, error_string) on failure.
    """
    try:
        uri = db_path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
    return {
        "balance_cents":       raw.get("current_balance_cents", 0),
        "daily_pnl_cents":     daily.get("gross_pnl_cents", 0),
        "daily_trades":        daily.get("trades_today", 0),
        "pause_until":         raw.get("drawdown_pause_until", None),
        "peak_balance_cents":  raw.get("peak_balance_cents", 0),
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
    Try pgrep to see if a bot_runner process is alive.
    Falls back to True if risk_state file exists (we can't pgrep on Windows).
    """
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", f"bot_runner.*{bot_name}"],
            capture_output=True, timeout=3
        )
        return result.returncode == 0
    except Exception:
        # Windows or pgrep unavailable — infer from file existence
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
    total_balance = 0
    total_pnl = 0

    for bot in BOTS:
        risk = read_risk_state(bot)
        status = read_status(bot)

        bal  = risk["balance_cents"]
        pnl  = risk["daily_pnl_cents"]
        total_balance += bal
        total_pnl     += pnl

        bots_data[bot] = {
            "balance_cents":  bal,
            "daily_pnl_cents": pnl,
            "daily_trades":   risk["daily_trades"],
            "max_trades":     8,
            "paused":         is_paused(risk["pause_until"]),
            "active":         bot_process_running(bot),
            "can_trade":      status.get("can_trade", True),
            "state":          status.get("state", "unknown"),
        }

    # portfolio_change_pct = daily_pnl / (portfolio - daily_pnl) * 100
    base = total_balance - total_pnl
    change_pct = round((total_pnl / base * 100) if base else 0.0, 2)

    return json_response({
        "portfolio_cents":    total_balance,
        "portfolio_change_pct": change_pct,
        "bots":               bots_data,
        "uptime_seconds":     get_uptime(),
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
        "approval_rate_pct": 0.0,
        "real_llm": 0, "quant_fallback": 0, "real_llm_pct": 0.0,
        "per_bot": {b: {"total": 0, "approved": 0} for b in BOTS},
    }
    clean_period = {
        "total_resolved": 0, "wins": 0, "win_rate_pct": 0.0, "start_date": today,
    }
    recent = []

    if conn is None:
        return json_response({"today": today_stats, "clean_period": clean_period,
                               "recent_decisions": recent, "error": err})

    try:
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
            "approval_rate_pct":  round(approved / total * 100, 1) if total else 0.0,
            "real_llm":           real_llm,
            "quant_fallback":     quant_fallback,
            "real_llm_pct":       round(real_llm / total * 100, 1) if total else 0.0,
            "per_bot":            per_bot,
        }

        # ── Clean-period stats (all resolved outcomes) ────────────────────
        # "Clean period" = all rows with a real outcome (win/loss), not quant fallback
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
            "win_rate_pct":    round(wins / total_res * 100, 1) if total_res else 0.0,
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

    except Exception as exc:
        traceback.print_exc()
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


# ── /api/positions ───────────────────────────────────────────────────────────

@app.route("/api/positions")
def api_positions():
    """
    Open position counts from risk_state and status files.
    """
    by_bot = {}
    total = 0

    for bot in BOTS:
        risk   = read_risk_state(bot)
        status = read_status(bot)
        # Prefer status file's open_positions, fall back to risk_state
        count = status.get("open_positions") or risk.get("open_positions", 0)
        by_bot[bot] = count
        total += count

    return json_response({"total_open": total, "by_bot": by_bot})


# ── /api/risk ────────────────────────────────────────────────────────────────

@app.route("/api/risk")
def api_risk():
    """
    Per-bot risk state and guardrail progress toward loosening thresholds.
    """
    bots_risk = {}
    for bot in BOTS:
        risk   = read_risk_state(bot)
        status = read_status(bot)

        bal  = risk["balance_cents"]
        peak = risk["peak_balance_cents"] or bal
        dd   = round((peak - bal) / peak * 100, 2) if peak else 0.0

        bots_risk[bot] = {
            "balance_cents":      bal,
            "peak_balance_cents": peak,
            "drawdown_pct":       dd,
            "daily_pnl_cents":    risk["daily_pnl_cents"],
            "daily_trades":       risk["daily_trades"],
            "can_trade":          status.get("can_trade", True),
            "paused":             is_paused(risk["pause_until"]),
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

    return json_response({"bots": bots_risk, "guardrail_progress": guardrail})


# ── /api/system ──────────────────────────────────────────────────────────────

@app.route("/api/system")
def api_system():
    """
    System health: Tavily usage, Anthropic status, uptime, log tail, health report.
    """
    # ── Log tail (last 10 lines) ──────────────────────────────────────────
    log_lines = []
    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as fh:
            log_lines = fh.readlines()
        log_lines = [l.rstrip() for l in log_lines[-10:]]
    except FileNotFoundError:
        log_lines = ["Log file not found"]
    except Exception as exc:
        log_lines = [f"Error reading log: {exc}"]

    # ── Tavily usage (best-effort grep through today's log lines) ─────────
    tavily_today = 0
    try:
        today_prefix = today_utc_str()
        with open(log_path(), "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if today_prefix not in line:
                    continue
                ll = line.lower()
                if "tavily" in ll and "exhausted" not in ll and "budget" not in ll and "error" not in ll:
                    tavily_today += 1
    except Exception:
        pass

    tavily_budget = 30
    tavily_pct = round(tavily_today / tavily_budget * 100, 1) if tavily_budget else 0.0

    # ── Anthropic status (401 errors in last hour of LLM decisions) ───────
    anthropic_status = "ok"
    conn, _ = open_db_readonly(llm_db_path())
    if conn:
        try:
            cur = conn.execute(
                "SELECT rationale FROM llm_decisions "
                "WHERE timestamp >= datetime('now', '-1 hour') "
                "ORDER BY id DESC LIMIT 100"
            )
            for r in cur.fetchall():
                if r["rationale"] and "401" in r["rationale"]:
                    anthropic_status = "error"
                    break
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
        "anthropic_status": anthropic_status,
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


# ── /api/equity ──────────────────────────────────────────────────────────────

@app.route("/api/equity")
def api_equity():
    """Return last 100 equity snapshots for the portfolio chart."""
    snapshots = read_json(data_path("equity_snapshots.json"), [])
    if not isinstance(snapshots, list):
        snapshots = []
    return json_response(snapshots[-100:])


# ── /api/control/pause/<bot> ─────────────────────────────────────────────────

@app.route("/api/control/pause/<bot_name>", methods=["POST", "OPTIONS"])
def api_pause(bot_name: str):
    """Write a pause signal file for the named bot."""
    if request.method == "OPTIONS":
        return json_response({})
    if bot_name not in BOTS:
        return json_response({"ok": False, "error": f"Unknown bot: {bot_name}"}, 400)
    signal = {"action": "pause", "timestamp": datetime.now(timezone.utc).isoformat()}
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
    signal = {"action": "resume", "timestamp": datetime.now(timezone.utc).isoformat()}
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


# ── /api/kill ────────────────────────────────────────────────────────────────

@app.route("/api/kill", methods=["POST", "OPTIONS"])
def api_kill():
    """
    Write data/kill_signal.json to request swarm shutdown.
    Requires body: {"confirm": "KILL"} to prevent accidents.
    """
    if request.method == "OPTIONS":
        return json_response({})
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "KILL":
        return json_response({"ok": False, "error": "Must send {\"confirm\": \"KILL\"}"}, 400)

    signal = {
        "action":    "kill",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "requested_by": "dashboard",
    }
    write_json(data_path("kill_signal.json"), signal)
    return json_response({"ok": True, "message": "Kill signal written to data/kill_signal.json"})


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

    print(f"[dashboard] Starting on http://{args.host}:{args.port}")
    print(f"[dashboard] Project root: {PROJECT_ROOT}")
    print(f"[dashboard] Data dir:     {PROJECT_ROOT / 'data'}")

    app.run(host=args.host, port=args.port, debug=False)
