#!/usr/bin/env python3
"""
System Performance Audit & Report Generator
Gathers exhaustive performance telemetry across:
1. Central LLM Brain (evaluations, latency, tokens, approval rate, categories, rejections)
2. Bot Execution & Trading (trade volume, positions, P&L, per-bot telemetry)
3. Market Scanner Throughput (scans per hour, filtering efficiency)
4. Research & Grounding Engine (Google Gemini search, fact extractions, confidence shifts)
5. Risk & Capital Utilization (balance tracking, drawdown, guard rails)
6. System Health, Uptime, Rate Limiting & Stability (HTTP 429 backoffs, exceptions)
"""

import glob
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path("d:/kalshi-swarm-new/Swarm-Kalshi")


def audit_llm_brain():
    db_path = PROJECT_ROOT / "data" / "central_llm.db"
    if not db_path.exists():
        return {"error": "central_llm.db not found"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM llm_decisions")
    total = cur.fetchone()[0]

    cur.execute("SELECT decision, COUNT(*) FROM llm_decisions GROUP BY decision")
    decision_counts = dict(cur.fetchall())

    cur.execute("SELECT bot_name, COUNT(*) FROM llm_decisions GROUP BY bot_name")
    by_bot = dict(cur.fetchall())

    cur.execute("SELECT timestamp, ticker, bot_name, decision, quant_confidence, llm_confidence, rationale, red_flags FROM llm_decisions ORDER BY id DESC LIMIT 10")
    recent = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT MIN(timestamp), MAX(timestamp) FROM llm_decisions")
    min_ts, max_ts = cur.fetchone()

    # Category analysis from tickers
    cur.execute("SELECT ticker FROM llm_decisions")
    tickers = [r[0] for r in cur.fetchall()]
    categories = Counter()
    for t in tickers:
        prefix = t.split("-")[0] if "-" in t else t[:6]
        categories[prefix] += 1

    # Red flag analysis
    cur.execute("SELECT red_flags FROM llm_decisions WHERE red_flags IS NOT NULL AND red_flags != ''")
    red_flag_rows = [r[0] for r in cur.fetchall()]
    red_flag_keywords = Counter()
    for rf in red_flag_rows:
        try:
            parsed = json.loads(rf)
            if isinstance(parsed, list):
                for item in parsed:
                    red_flag_keywords[str(item)[:50]] += 1
            else:
                red_flag_keywords[str(parsed)[:50]] += 1
        except Exception:
            red_flag_keywords[str(rf)[:50]] += 1

    conn.close()

    time_span_hours = 0
    if min_ts and max_ts:
        try:
            t0 = datetime.fromisoformat(min_ts)
            t1 = datetime.fromisoformat(max_ts)
            time_span_hours = max(0.1, (t1 - t0).total_seconds() / 3600.0)
        except Exception:
            pass

    return {
        "total_evaluations": total,
        "decision_counts": decision_counts,
        "approval_rate": round(decision_counts.get("approve", 0) / max(1, total) * 100, 2),
        "rejection_rate": round(decision_counts.get("reject", 0) / max(1, total) * 100, 2),
        "evaluations_per_hour": round(total / max(0.1, time_span_hours), 2),
        "time_span_hours": round(time_span_hours, 2),
        "earliest_decision": min_ts,
        "latest_decision": max_ts,
        "evaluations_by_bot": by_bot,
        "top_market_series": dict(categories.most_common(8)),
        "top_red_flags": dict(red_flag_keywords.most_common(5)),
        "recent_sample": recent[:5],
    }


def audit_bot_databases():
    bots = ["sentinel", "oracle", "pulse", "vanguard"]
    results = {}
    total_trades = 0

    for bot in bots:
        db_path = PROJECT_ROOT / "data" / f"{bot}.db"
        if not db_path.exists():
            results[bot] = {"status": "missing"}
            continue
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        
        trade_count = 0
        trades_list = []
        if "trades" in tables:
            cur.execute("SELECT COUNT(*) FROM trades")
            trade_count = cur.fetchone()[0]
            total_trades += trade_count
            cur.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 5")
            trades_list = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
        
        results[bot] = {
            "tables": tables,
            "trade_count": trade_count,
            "recent_trades": trades_list,
        }
        conn.close()

    return {"bots": results, "total_recorded_trades": total_trades}


def audit_risk_states():
    bots = ["sentinel", "oracle", "pulse", "vanguard"]
    states = {}
    for bot in bots:
        state_file = PROJECT_ROOT / "data" / f"{bot}_risk_state.json"
        if state_file.exists():
            try:
                with open(state_file, "r") as f:
                    states[bot] = json.load(f)
            except Exception as e:
                states[bot] = {"error": str(e)}
        else:
            states[bot] = {"status": "not_found"}
    return states


def audit_system_logs():
    log_file = PROJECT_ROOT / "logs" / "swarm.log"
    if not log_file.exists():
        return {"error": "swarm.log not found"}

    scan_count = 0
    grounding_searches = 0
    rate_limit_429s = 0
    http_errors = Counter()
    log_lines = 0

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                log_lines += 1
                if "Scanning active markets" in line:
                    scan_count += 1
                if "grounding" in line.lower() or "gemini" in line.lower() and "search" in line.lower():
                    grounding_searches += 1
                if "429" in line or "rate limit" in line.lower():
                    rate_limit_429s += 1
                if "HTTP " in line:
                    for part in line.split():
                        if part.startswith("4") or part.startswith("5"):
                            if len(part) == 3 and part.isdigit():
                                http_errors[part] += 1
    except Exception as e:
        return {"error": str(e)}

    return {
        "total_log_lines_sampled": log_lines,
        "market_scans_logged": scan_count,
        "grounding_searches_logged": grounding_searches,
        "rate_limit_429_events": rate_limit_429s,
        "http_error_distribution": dict(http_errors),
    }


def main():
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "llm_brain": audit_llm_brain(),
        "trading_engine": audit_bot_databases(),
        "risk_states": audit_risk_states(),
        "system_telemetry": audit_system_logs(),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
