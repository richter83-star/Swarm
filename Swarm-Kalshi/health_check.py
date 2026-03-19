#!/usr/bin/env python3
"""
health_check.py
===============

Daily health check and auto-repair system for the Kalshi trading bot swarm.

Run manually : python health_check.py
Run via cron  : see setup_cron.sh

Performs 10 checks, auto-fixes where safe, writes two JSON reports, sends
a Telegram notification (if configured), and writes an audit log entry to
the dashboard DB.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Optional psutil (graceful fallback if not installed)
# ---------------------------------------------------------------------------
try:
    import psutil
    _PSUTIL = True
except ImportError:
    psutil = None  # type: ignore
    _PSUTIL = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH  = PROJECT_ROOT / "config" / "swarm_config.yaml"
DATA_DIR     = PROJECT_ROOT / "data"
LOG_DIR      = PROJECT_ROOT / "logs"
REPORTS_DIR  = DATA_DIR / "health_reports"
DASH_DB_PATH = DATA_DIR / "dashboard.db"

BOT_NAMES = ["sentinel", "oracle", "pulse", "vanguard"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _get_bot_db_path(bot_name: str) -> Path:
    """Resolve a bot's SQLite DB path from its config (same logic as dashboard)."""
    bot_cfg_path = PROJECT_ROOT / "config" / f"{bot_name}_config.yaml"
    if bot_cfg_path.exists():
        try:
            with open(bot_cfg_path, "r", encoding="utf-8") as fh:
                bot_cfg = yaml.safe_load(fh) or {}
            db_rel = bot_cfg.get("learning", {}).get("db_path", f"data/{bot_name}.db")
            return PROJECT_ROOT / db_rel
        except Exception:
            pass
    return PROJECT_ROOT / "data" / f"{bot_name}.db"


def _open_db(path: Path, timeout: int = 10) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(str(path), timeout=timeout)
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn
    except Exception:
        return None


def _status_path(bot_name: str) -> Path:
    return DATA_DIR / f"{bot_name}_status.json"


def _risk_state_path(bot_name: str) -> Path:
    return DATA_DIR / f"{bot_name}_risk_state.json"


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)


# ---------------------------------------------------------------------------
# Ticker date parser
# ---------------------------------------------------------------------------
# Ticker format example: KXEOWEEK-26MAR14
# The date suffix is YYMONDD  e.g. 26MAR14 = March 14, 2026

_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ticker_date(ticker: str) -> Optional[datetime]:
    """
    Extract the expiry date from a Kalshi ticker like KXEOWEEK-26MAR14.
    Returns a UTC datetime at midnight of that date, or None if unparseable.
    """
    import re
    # Pattern: two digits year, three-letter month, two digits day at the end
    # May appear after a dash or directly in the ticker
    m = re.search(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})", ticker.upper())
    if not m:
        return None
    try:
        yy, mon_str, dd = int(m.group(1)), m.group(2), int(m.group(3))
        year = 2000 + yy
        month = _MONTH_MAP.get(mon_str)
        if not month:
            return None
        return datetime(year, month, dd, 0, 0, 0, tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CHECK 1 — Stale trade detection & fix
# ---------------------------------------------------------------------------

def check_stale_trades(actions: List[str]) -> dict:
    today = _now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    expiry_cutoff = today - timedelta(days=1)   # expiry + 1 day < today

    total_fixed = 0
    details_parts = []

    for bot in BOT_NAMES:
        db_path = _get_bot_db_path(bot)
        if not db_path.exists():
            continue
        conn = _open_db(db_path)
        if not conn:
            continue
        try:
            # Check that the trades table has required columns
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
            if "status" not in cols and "outcome" not in cols:
                continue

            # Pending trades: outcome IS NULL or outcome = 'pending'
            pending_q = """
                SELECT id, ticker, timestamp
                FROM trades
                WHERE (outcome IS NULL OR outcome = 'pending')
                  AND fill_status != 'stale_expired'
            """
            rows = conn.execute(pending_q).fetchall()

            bot_fixed = 0
            for trade_id, ticker, ts in rows:
                if not ticker:
                    continue
                exp_date = _parse_ticker_date(ticker)
                if exp_date is None:
                    continue
                # Stale if expiry date + 1 day is before today
                if exp_date + timedelta(days=1) < today:
                    conn.execute(
                        """UPDATE trades
                           SET fill_status = 'stale_expired',
                               outcome = 'expired',
                               pnl_cents = 0,
                               settled_at = ?
                           WHERE id = ?""",
                        (_now_utc().isoformat(), trade_id),
                    )
                    actions.append(
                        f"Fixed stale trade id={trade_id} {ticker} in {bot}.db"
                    )
                    bot_fixed += 1

            if bot_fixed:
                conn.commit()
                details_parts.append(f"Fixed {bot_fixed} stale trade(s) in {bot}.db")
            total_fixed += bot_fixed
        except Exception as exc:
            details_parts.append(f"Error checking {bot}.db: {exc}")
        finally:
            conn.close()

    if total_fixed:
        status = "FIXED"
        details = "; ".join(details_parts) if details_parts else f"Fixed {total_fixed} stale trade(s)"
    else:
        status = "OK"
        details = "No stale trades found" + ("; " + "; ".join(details_parts) if details_parts else "")

    return {"status": status, "details": details, "auto_fixed": total_fixed > 0, "count": total_fixed}


# ---------------------------------------------------------------------------
# CHECK 2 — Duplicate process detection
# ---------------------------------------------------------------------------

def check_duplicate_processes() -> dict:
    if not _PSUTIL:
        return {"status": "SKIP", "details": "psutil not installed — skipping process check", "counts": {}}

    targets = ["swarm_daemon.py", "run_swarm.py"] + [f"{b}_runner" for b in BOT_NAMES]
    counts: Dict[str, int] = {}
    issues = []

    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
                for target in targets:
                    if target in cmdline:
                        counts[target] = counts.get(target, 0) + 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        for target, cnt in counts.items():
            if cnt > 1:
                issues.append(f"{target} running {cnt} times")

        if issues:
            status = "CRITICAL"
            details = "Duplicate processes: " + "; ".join(issues)
        else:
            status = "OK"
            details = "All process counts normal" + (
                f" ({', '.join(f'{k}:1' for k in counts)})" if counts else " (no target processes found)"
            )
    except Exception as exc:
        status = "WARNING"
        details = f"Process check error: {exc}"

    return {"status": status, "details": details, "counts": counts}


# ---------------------------------------------------------------------------
# CHECK 3 — P&L anomaly check
# ---------------------------------------------------------------------------

def check_pnl_anomalies() -> dict:
    cutoff_7d = (_now_utc() - timedelta(days=7)).isoformat()
    total_recent = 0
    total_all = 0
    parts = []

    for bot in BOT_NAMES:
        db_path = _get_bot_db_path(bot)
        if not db_path.exists():
            continue
        conn = _open_db(db_path)
        if not conn:
            continue
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
            if "pnl_valid" not in cols:
                continue

            all_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE pnl_valid = 0"
            ).fetchone()[0]
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE pnl_valid = 0 AND timestamp >= ?",
                (cutoff_7d,),
            ).fetchone()[0]
            if all_count:
                parts.append(f"{bot}: {recent_count} recent / {all_count} total anomalies")
            total_recent += recent_count
            total_all += all_count
        except Exception as exc:
            parts.append(f"{bot} error: {exc}")
        finally:
            conn.close()

    if total_recent > 5:
        status = "WARNING"
    elif total_recent > 0:
        status = "INFO"
    else:
        status = "OK"

    if parts:
        details = "; ".join(parts)
    else:
        details = f"No P&L anomalies found (checked last 7 days)"

    return {
        "status": status,
        "details": details,
        "anomalies_7d": total_recent,
        "anomalies_total": total_all,
    }


# ---------------------------------------------------------------------------
# CHECK 4 — Balance & drawdown check
# ---------------------------------------------------------------------------

def check_balance_drawdown(actions: List[str]) -> dict:
    issues = []
    info_parts = []

    for bot in BOT_NAMES:
        risk_path = _risk_state_path(bot)
        if not risk_path.exists():
            info_parts.append(f"{bot}: no risk_state file")
            continue
        try:
            state = _load_json(risk_path)
            balance = state.get("current_balance_cents", 0)
            peak = state.get("peak_balance_cents", 0)

            if peak and balance > peak:
                # Balance grew above tracked peak — reset peak
                state["peak_balance_cents"] = balance
                state["saved_at"] = _now_utc().isoformat()
                _save_json(risk_path, state)
                actions.append(f"Auto-reset drawdown peak for {bot} to {balance}¢")
                info_parts.append(f"{bot}: auto-reset peak to {balance}¢")
                peak = balance  # use updated value for drawdown calc

            if peak and balance:
                drawdown_pct = (peak - balance) / peak * 100
                if drawdown_pct > 30:
                    issues.append(f"{bot} drawdown {drawdown_pct:.1f}% (> 30%)")
                else:
                    info_parts.append(f"{bot}: drawdown {drawdown_pct:.1f}%")
            else:
                info_parts.append(f"{bot}: balance={balance}¢ peak={peak}¢")
        except Exception as exc:
            info_parts.append(f"{bot} error: {exc}")

    if issues:
        status = "WARNING"
        details = "Drawdown issues: " + "; ".join(issues)
        if info_parts:
            details += " | " + "; ".join(info_parts)
    else:
        status = "OK"
        details = "; ".join(info_parts) if info_parts else "All drawdowns within limits"

    return {"status": status, "details": details, "issues": issues}


# ---------------------------------------------------------------------------
# CHECK 5 — Config consistency check
# ---------------------------------------------------------------------------

def check_config_consistency(cfg: dict) -> dict:
    issues = []
    info_parts = []

    try:
        trading = cfg.get("trading", {})
        learning = cfg.get("learning", {})

        t_conf = trading.get("min_confidence_threshold")
        l_conf = learning.get("min_confidence_threshold")
        if t_conf is not None and l_conf is not None and t_conf != l_conf:
            issues.append(
                f"min_confidence_threshold mismatch: trading={t_conf} vs learning={l_conf}"
            )
        else:
            info_parts.append(f"min_confidence_threshold={t_conf} (consistent)")

        max_pos_pct = trading.get("max_position_pct")
        if max_pos_pct is not None:
            if not (0.01 <= float(max_pos_pct) <= 0.10):
                issues.append(
                    f"max_position_pct={max_pos_pct} outside sane range [0.01, 0.10]"
                )
            else:
                info_parts.append(f"max_position_pct={max_pos_pct} OK")

        max_open = trading.get("max_open_positions")
        if max_open is not None:
            if not (1 <= int(max_open) <= 20):
                issues.append(
                    f"max_open_positions={max_open} outside sane range [1, 20]"
                )
            else:
                info_parts.append(f"max_open_positions={max_open} OK")

    except Exception as exc:
        issues.append(f"Config parse error: {exc}")

    if issues:
        status = "WARNING"
        details = "Config issues: " + "; ".join(issues)
    else:
        status = "OK"
        details = "All config values consistent" + (
            f" ({', '.join(info_parts)})" if info_parts else ""
        )

    return {"status": status, "details": details, "issues": issues}


# ---------------------------------------------------------------------------
# CHECK 6 — Database integrity check
# ---------------------------------------------------------------------------

def check_db_integrity() -> dict:
    issues = []
    ok_parts = []

    for bot in BOT_NAMES:
        db_path = _get_bot_db_path(bot)
        if not db_path.exists():
            ok_parts.append(f"{bot}.db not found (skipped)")
            continue
        conn = _open_db(db_path)
        if not conn:
            issues.append(f"Cannot open {bot}.db")
            continue
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            if result and result[0] != "ok":
                issues.append(f"{bot}.db integrity FAIL: {result[0]}")
            else:
                ok_parts.append(f"{bot}.db OK")

            # Flush WAL file
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception as exc:
            issues.append(f"{bot}.db check error: {exc}")
        finally:
            conn.close()

    if issues:
        status = "CRITICAL"
        details = "DB issues: " + "; ".join(issues)
    else:
        status = "OK"
        details = "All databases healthy (" + ", ".join(ok_parts) + ")"

    return {"status": status, "details": details, "issues": issues}


# ---------------------------------------------------------------------------
# CHECK 7 — Log size check
# ---------------------------------------------------------------------------

def check_log_size(actions: List[str]) -> dict:
    MAX_BYTES = 50 * 1024 * 1024  # 50 MB
    log_path = LOG_DIR / "swarm.log"
    parts = []
    rotated = False

    try:
        if log_path.exists():
            size = log_path.stat().st_size
            size_mb = size / (1024 * 1024)
            if size > MAX_BYTES:
                old_path = LOG_DIR / "swarm.log.old"
                shutil.move(str(log_path), str(old_path))
                log_path.touch()
                rotated = True
                actions.append(f"Rotated swarm.log ({size_mb:.1f}MB) to swarm.log.old")
                parts.append(f"swarm.log rotated ({size_mb:.1f}MB > 50MB)")
            else:
                parts.append(f"swarm.log: {size_mb:.1f}MB")
        else:
            parts.append("swarm.log: not found")

        # Also report daemon.log size
        daemon_log = LOG_DIR / "daemon.log"
        if daemon_log.exists():
            size_mb = daemon_log.stat().st_size / (1024 * 1024)
            parts.append(f"daemon.log: {size_mb:.1f}MB")

    except Exception as exc:
        parts.append(f"Log check error: {exc}")

    status = "FIXED" if rotated else "OK"
    return {
        "status": status,
        "details": "; ".join(parts),
        "auto_fixed": rotated,
    }


# ---------------------------------------------------------------------------
# CHECK 8 — Dead bot detection
# ---------------------------------------------------------------------------

def check_dead_bots() -> dict:
    issues = []
    ok_parts = []

    for bot in BOT_NAMES:
        st_path = _status_path(bot)
        if not st_path.exists():
            ok_parts.append(f"{bot}: no status file")
            continue
        try:
            status_data = _load_json(st_path)
            reported_state = status_data.get("state", "unknown")
            pid = status_data.get("pid")

            if pid is None:
                ok_parts.append(f"{bot}: no PID in status")
                continue

            pid = int(pid)
            alive = False
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, OSError):
                alive = False
            except PermissionError:
                alive = True  # exists but we can't signal it

            if reported_state == "running" and not alive:
                issues.append(
                    f"{bot} PID {pid} is dead but status shows 'running'"
                )
            elif alive:
                ok_parts.append(f"{bot} PID {pid} alive")
            else:
                ok_parts.append(f"{bot} PID {pid} not running (state={reported_state})")

        except Exception as exc:
            ok_parts.append(f"{bot} status check error: {exc}")

    if issues:
        status = "CRITICAL"
        details = "Dead bots: " + "; ".join(issues)
    else:
        status = "OK"
        details = "; ".join(ok_parts) if ok_parts else "All bots status OK"

    return {"status": status, "details": details, "issues": issues}


# ---------------------------------------------------------------------------
# CHECK 9 — Win rate check
# ---------------------------------------------------------------------------

def check_win_rates(recommendations: List[str]) -> Tuple[dict, Dict[str, dict]]:
    """
    Returns (check_result, bot_summary_dict).
    bot_summary is populated here as a side-effect of the DB queries.
    """
    warnings = []
    info_parts = []
    bot_summary: Dict[str, dict] = {}

    for bot in BOT_NAMES:
        db_path = _get_bot_db_path(bot)
        st_path = _status_path(bot)
        status_data = _load_json(st_path) if st_path.exists() else {}
        bot_state = status_data.get("state", "unknown")

        entry: Dict[str, Any] = {
            "trades": 0,
            "win_rate": 0.0,
            "pending": 0,
            "pnl_cents": 0,
            "status": bot_state,
        }

        if not db_path.exists():
            bot_summary[bot] = entry
            continue

        conn = _open_db(db_path)
        if not conn:
            bot_summary[bot] = entry
            continue

        try:
            # Total trades
            total = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE outcome IS NOT NULL AND outcome != 'pending'"
            ).fetchone()[0]

            # Last 50 trades for win rate
            last_50 = conn.execute(
                """SELECT outcome FROM trades
                   WHERE outcome IS NOT NULL AND outcome != 'pending'
                   ORDER BY id DESC LIMIT 50"""
            ).fetchall()
            wins_50 = sum(1 for (o,) in last_50 if o == "win")
            win_rate = (wins_50 / len(last_50) * 100) if last_50 else 0.0

            # Pending count
            pending = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE outcome IS NULL OR outcome = 'pending'"
            ).fetchone()[0]

            # Total P&L
            pnl_row = conn.execute(
                "SELECT COALESCE(SUM(pnl_cents), 0) FROM trades WHERE pnl_cents IS NOT NULL"
            ).fetchone()
            pnl_cents = pnl_row[0] if pnl_row else 0

            entry.update({
                "trades": total,
                "win_rate": round(win_rate, 2),
                "pending": pending,
                "pnl_cents": pnl_cents,
            })

            # Win rate alerts
            if len(last_50) >= 10:
                if win_rate < 35:
                    warnings.append(
                        f"{bot.capitalize()} win rate {win_rate:.1f}% — critically low"
                    )
                    recommendations.append(
                        f"{bot.capitalize()} win rate critically low ({win_rate:.1f}%) — consider pausing and reviewing strategy"
                    )
                elif win_rate > 60:
                    info_parts.append(
                        f"{bot.capitalize()} win rate {win_rate:.1f}% — performing well"
                    )
            elif total == 0:
                info_parts.append(f"{bot.capitalize()}: no closed trades yet")
            else:
                info_parts.append(f"{bot.capitalize()}: {win_rate:.1f}% win rate ({len(last_50)} trades)")

        except Exception as exc:
            info_parts.append(f"{bot} error: {exc}")
        finally:
            conn.close()

        bot_summary[bot] = entry

    if warnings:
        status = "WARNING"
        details = "; ".join(warnings)
    else:
        status = "OK"
        details = "; ".join(info_parts) if info_parts else "All win rates within normal range"

    return (
        {"status": status, "details": details, "warnings": warnings},
        bot_summary,
    )


# ---------------------------------------------------------------------------
# CHECK 10 — Memory usage check
# ---------------------------------------------------------------------------

def check_memory_usage() -> dict:
    if not _PSUTIL:
        return {"status": "SKIP", "details": "psutil not installed — skipping memory check", "usage_mb": {}}

    MAX_MB = 500
    usage: Dict[str, float] = {}
    issues = []

    try:
        targets = ["swarm_daemon.py", "run_swarm.py", "bot_runner"]
        for proc in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
                for target in targets:
                    if target in cmdline:
                        mem_mb = proc.info["memory_info"].rss / (1024 * 1024)
                        label = f"{target}[{proc.info['pid']}]"
                        usage[label] = round(mem_mb, 1)
                        if mem_mb > MAX_MB:
                            issues.append(f"{label} using {mem_mb:.0f}MB (> {MAX_MB}MB)")
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass
    except Exception as exc:
        return {"status": "WARNING", "details": f"Memory check error: {exc}", "usage_mb": {}}

    if issues:
        status = "WARNING"
        details = "High memory: " + "; ".join(issues)
    else:
        if usage:
            max_usage = max(usage.values())
            max_proc = max(usage, key=usage.get)
            details = f"Max usage: {max_proc} {max_usage:.0f}MB"
        else:
            details = "No target processes found to measure"
        status = "OK"

    return {"status": status, "details": details, "usage_mb": usage}


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def _send_telegram(cfg: dict, report: dict, bot_summary: Dict[str, dict]) -> None:
    tg = cfg.get("telegram", {})
    if not tg.get("enabled", False):
        return

    bot_token = tg.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = tg.get("chat_id")   or os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        return

    overall = report.get("overall_status", "UNKNOWN")
    date_str = _now_utc().strftime("%Y-%m-%d")

    if overall == "OK":
        overall_icon = "✅ OK"
    elif overall == "WARNING":
        overall_icon = "⚠️ WARNING"
    else:
        overall_icon = "🚨 CRITICAL"

    actions = report.get("actions_taken", [])
    recs    = report.get("recommendations", [])

    lines = [f"🤖 Kalshi Swarm Health Report — {date_str}", "", f"Overall: {overall_icon}", ""]

    if actions:
        lines.append("Fixes applied:")
        for a in actions[:5]:
            lines.append(f"• {a}")
        lines.append("")

    if recs:
        lines.append("Warnings:")
        for r in recs[:5]:
            lines.append(f"• {r}")
        lines.append("")

    # Bot status line
    bot_icons = []
    total_balance = 0
    total_pnl = 0
    for b in BOT_NAMES:
        st = bot_summary.get(b, {}).get("status", "unknown")
        icon = "✅" if st == "running" else "❌"
        bot_icons.append(f"{b} {icon}")

    lines.append("Bots: " + "  ".join(bot_icons))

    # Balance and P&L from risk states
    for b in BOT_NAMES:
        rp = _risk_state_path(b)
        if rp.exists():
            rs = _load_json(rp)
            total_balance += rs.get("current_balance_cents", 0)
            total_pnl     += rs.get("daily", {}).get("gross_pnl_cents", 0)

    balance_str = f"${total_balance / 100:.2f}"
    pnl_sign    = "+" if total_pnl >= 0 else ""
    pnl_str     = f"{pnl_sign}${total_pnl / 100:.2f}"
    lines.append(f"Balance: {balance_str} | P&L today: {pnl_str}")
    lines.append("")
    lines.append("Full report: data/health_report_latest.json")

    message = "\n".join(lines)

    import urllib.request
    import urllib.parse
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = int(tg.get("timeout_seconds", 8))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            pass  # success
    except Exception as exc:
        print(f"[health_check] Telegram send failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Audit log entry
# ---------------------------------------------------------------------------

def _write_audit_log(report: dict) -> None:
    try:
        DASH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DASH_DB_PATH), timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT,
                detail TEXT
            )
        """)
        summary = {
            "overall_status": report.get("overall_status"),
            "actions_taken": report.get("actions_taken", []),
            "recommendations": report.get("recommendations", []),
            "summary": report.get("summary"),
        }
        conn.execute(
            "INSERT INTO audit_log (created_at, action, target, detail) VALUES (?,?,?,?)",
            (
                _now_utc().isoformat(),
                "health_check",
                "swarm",
                json.dumps(summary),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[health_check] Audit log write failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Overall status aggregation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Check 11: Dashboard process alive
# ---------------------------------------------------------------------------
def check_dashboard_alive() -> dict:
    """Verify the dashboard web server is responding on port 8080."""
    import socket
    import subprocess

    port = 8080
    host = "127.0.0.1"

    def _port_open() -> bool:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError:
            return False

    if _port_open():
        return {"status": "OK", "details": f"Dashboard responding on {host}:{port}"}

    # Not responding — try to restart via launch.sh
    try:
        subprocess.Popen(
            ["bash", str(PROJECT_ROOT / "launch.sh")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
        )
        auto_fixed = True
        detail = f"Dashboard NOT responding on :{port} — launch.sh triggered to restart"
    except Exception as exc:
        auto_fixed = False
        detail = f"Dashboard NOT responding on :{port} — auto-restart failed: {exc}"

    return {
        "status": "CRITICAL",
        "details": detail,
        "auto_fixed": auto_fixed,
    }


# ---------------------------------------------------------------------------
# Check 12: Bot DB freshness (written to recently)
# ---------------------------------------------------------------------------
def check_bot_db_freshness() -> dict:
    """Ensure each bot is actively cycling by checking risk_state.json freshness.

    Bots only write to their SQLite DB on trade events, but they write
    risk_state.json on every scan cycle (~60s). Stale risk_state = dead/stuck bot.
    Also verifies the bot DB file itself exists and is readable.
    """
    import time

    stale_threshold_secs = 3 * 3600  # 3 hours — bot should cycle at least once
    now_ts = time.time()
    stale = []
    ok_parts = []

    for bot in BOT_NAMES:
        # 1. Check risk_state.json (updated every cycle)
        rs_path = PROJECT_ROOT / "data" / f"{bot}_risk_state.json"
        if not rs_path.exists():
            stale.append(f"{bot}: risk_state.json missing")
            continue
        age_secs = now_ts - rs_path.stat().st_mtime
        age_min  = int(age_secs / 60)
        if age_secs > stale_threshold_secs:
            stale.append(f"{bot}: risk_state last updated {age_min}m ago — bot may be stuck")
            continue

        # 2. Confirm DB file exists and is readable
        db_path = _get_bot_db_path(bot)
        if not db_path.exists():
            stale.append(f"{bot}: DB file missing at {db_path}")
            continue

        ok_parts.append(f"{bot}: active (risk_state {age_min}m ago, DB {db_path.stat().st_size//1024}KB)")

    if stale:
        return {
            "status": "WARNING",
            "details": "Stale/missing bots: " + "; ".join(stale),
            "stale_bots": stale,
        }
    return {
        "status": "OK",
        "details": "; ".join(ok_parts),
    }


def _aggregate_status(checks: dict) -> str:
    order = {"CRITICAL": 3, "WARNING": 2, "FIXED": 1, "INFO": 1, "OK": 0, "SKIP": 0}
    worst = "OK"
    for check in checks.values():
        s = check.get("status", "OK")
        if order.get(s, 0) > order.get(worst, 0):
            worst = s
    if worst in ("FIXED", "INFO"):
        return "OK"
    return worst


def _build_summary(checks: dict, actions: List[str], recommendations: List[str]) -> str:
    parts = []
    if actions:
        parts.append(f"{len(actions)} fix(es) applied")
    if recommendations:
        parts.append(f"{len(recommendations)} warning(s)")

    stale_count = checks.get("stale_trades", {}).get("count", 0)
    if stale_count:
        parts.append(f"{stale_count} stale trade(s) fixed")

    win_warnings = checks.get("win_rates", {}).get("warnings", [])
    if win_warnings:
        parts.append(f"Win rate issues: {len(win_warnings)} bot(s)")

    if not parts:
        return "All systems healthy, no issues detected"
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_health_check() -> dict:
    now = _now_utc()
    date_str = now.strftime("%Y-%m-%d")

    print(f"[health_check] Starting health check at {now.isoformat()}")

    cfg = _load_config()
    actions: List[str] = []
    recommendations: List[str] = []

    checks: Dict[str, dict] = {}

    # --- Check 1: Stale trades ---
    print("[health_check] Check 1/12: Stale trades...")
    try:
        checks["stale_trades"] = check_stale_trades(actions)
    except Exception as exc:
        checks["stale_trades"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
            "auto_fixed": False,
        }
        traceback.print_exc()

    # --- Check 2: Duplicate processes ---
    print("[health_check] Check 2/12: Duplicate processes...")
    try:
        checks["duplicate_processes"] = check_duplicate_processes()
    except Exception as exc:
        checks["duplicate_processes"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 3: P&L anomalies ---
    print("[health_check] Check 3/12: P&L anomalies...")
    try:
        checks["pnl_anomalies"] = check_pnl_anomalies()
    except Exception as exc:
        checks["pnl_anomalies"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 4: Balance & drawdown ---
    print("[health_check] Check 4/12: Balance & drawdown...")
    try:
        checks["balance_drawdown"] = check_balance_drawdown(actions)
    except Exception as exc:
        checks["balance_drawdown"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 5: Config consistency ---
    print("[health_check] Check 5/12: Config consistency...")
    try:
        checks["config_consistency"] = check_config_consistency(cfg)
    except Exception as exc:
        checks["config_consistency"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 6: DB integrity ---
    print("[health_check] Check 6/12: Database integrity...")
    try:
        checks["db_integrity"] = check_db_integrity()
    except Exception as exc:
        checks["db_integrity"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 7: Log size ---
    print("[health_check] Check 7/12: Log sizes...")
    try:
        checks["log_size"] = check_log_size(actions)
    except Exception as exc:
        checks["log_size"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 8: Dead bots ---
    print("[health_check] Check 8/12: Dead bot detection...")
    try:
        checks["dead_bots"] = check_dead_bots()
    except Exception as exc:
        checks["dead_bots"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 9: Win rates (also builds bot_summary) ---
    print("[health_check] Check 9/12: Win rates...")
    bot_summary: Dict[str, dict] = {}
    try:
        win_check, bot_summary = check_win_rates(recommendations)
        checks["win_rates"] = win_check
    except Exception as exc:
        checks["win_rates"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }
        traceback.print_exc()

    # --- Check 10: Memory usage ---
    print("[health_check] Check 10/12: Memory usage...")
    try:
        checks["memory"] = check_memory_usage()
    except Exception as exc:
        checks["memory"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 11: Dashboard alive ---
    print("[health_check] Check 11/12: Dashboard alive...")
    try:
        checks["dashboard_alive"] = check_dashboard_alive()
    except Exception as exc:
        checks["dashboard_alive"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Check 12: Bot DB freshness ---
    print("[health_check] Check 12/12: Bot DB freshness...")
    try:
        checks["bot_db_freshness"] = check_bot_db_freshness()
    except Exception as exc:
        checks["bot_db_freshness"] = {
            "status": "WARNING",
            "details": f"Check failed: {exc}",
        }

    # --- Additional recommendations ---
    for bot, info in bot_summary.items():
        if info.get("win_rate", 100) == 0.0 and info.get("trades", 0) >= 10:
            recommendations.append(
                f"{bot.capitalize()} 0% win rate on {info['trades']} trades — monitor closely"
            )

    # --- Assemble report ---
    overall_status = _aggregate_status(checks)
    summary_str    = _build_summary(checks, actions, recommendations)

    report = {
        "generated_at": now.isoformat(),
        "system": "Kalshi Swarm v2",
        "overall_status": overall_status,
        "summary": summary_str,
        "checks": checks,
        "bot_summary": bot_summary,
        "actions_taken": actions,
        "recommendations": recommendations,
    }

    # --- Write reports ---
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    dated_path  = REPORTS_DIR / f"health_report_{date_str}.json"
    latest_path = DATA_DIR / "health_report_latest.json"

    _save_json(dated_path, report)
    _save_json(latest_path, report)
    print(f"[health_check] Reports written: {dated_path.name} + health_report_latest.json")

    # --- Audit log ---
    try:
        _write_audit_log(report)
        print("[health_check] Audit log entry written to dashboard.db")
    except Exception as exc:
        print(f"[health_check] Audit log failed: {exc}", file=sys.stderr)

    # --- Telegram ---
    try:
        _send_telegram(cfg, report, bot_summary)
        if cfg.get("telegram", {}).get("enabled", False):
            print("[health_check] Telegram notification sent")
    except Exception as exc:
        print(f"[health_check] Telegram failed: {exc}", file=sys.stderr)

    # --- Console summary ---
    print("\n" + "=" * 60)
    print(f"  HEALTH CHECK COMPLETE — {overall_status}")
    print("=" * 60)
    print(f"  Summary : {summary_str}")
    print(f"  Actions : {len(actions)}")
    print(f"  Warnings: {len(recommendations)}")
    for check_name, result in checks.items():
        s = result.get("status", "?")
        d = result.get("details", "")[:80]
        print(f"  [{s:8s}] {check_name}: {d}")
    print("=" * 60)
    print(f"  Full report: {latest_path}")
    print("=" * 60 + "\n")

    return report


if __name__ == "__main__":
    run_health_check()
