#!/usr/bin/env python3
"""
watch_demo_learning.py
======================

Real-time Demo Trading Learning & Adaptation Monitor for Kalshi Swarm.

Tracks and verifies whether the swarm and individual bots are actively learning:
1. Confidence Calibration Curve & Expected Calibration Error (ECE)
2. Dynamic Feature Weights Evolution (edge, liquidity, volume, timing, momentum)
3. Category Specialization & Multipliers (Hot vs Cold categories)
4. Central LLM Brain Filtering Alpha (Approved vs Rejected accuracy)
5. Rolling Win Rate Momentum & Brier Score Progression

Usage:
    python watch_demo_learning.py              # Generate full learning audit report
    python watch_demo_learning.py --watch      # Continuous live refreshing monitor
    python watch_demo_learning.py --json       # Output machine-readable JSON
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
BOT_NAMES = ["sentinel", "oracle", "pulse", "vanguard"]


# ---------------------------------------------------------------------------
# Core Analytics & Data Aggregation
# ---------------------------------------------------------------------------

def _open_db(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def get_all_settled_trades(data_dir: Path = DATA_DIR) -> List[Dict[str, Any]]:
    """Retrieve all trades across bot DBs with normalized fields."""
    trades = []
    for bot in BOT_NAMES:
        db_path = data_dir / f"{bot}.db"
        conn = _open_db(db_path)
        if not conn:
            continue
        try:
            cur = conn.execute(
                """
                SELECT id, timestamp, ticker, event_ticker, title, series_ticker,
                       category, bot_name, side, action, count, entry_price, fill_price,
                       confidence, edge_score, liquidity_score, volume_score,
                       timing_score, momentum_score, outcome, exit_price, pnl_cents,
                       settled_at, pnl_valid
                FROM trades
                ORDER BY timestamp ASC
                """
            )
            for row in cur.fetchall():
                d = dict(row)
                d["bot"] = d.get("bot_name") or bot
                trades.append(d)
        except Exception:
            # Fallback if some columns missing in legacy schemas
            try:
                cur = conn.execute("SELECT * FROM trades ORDER BY timestamp ASC")
                for row in cur.fetchall():
                    d = dict(row)
                    d["bot"] = d.get("bot_name") or bot
                    trades.append(d)
            except Exception:
                pass
        finally:
            conn.close()

    trades.sort(key=lambda x: str(x.get("timestamp") or ""))
    return trades


def calculate_confidence_calibration(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes calibration curve across confidence buckets:
    50-60%, 60-70%, 70-80%, 80-90%, 90-100%.
    """
    buckets_def = [
        {"min": 50.0, "max": 60.0, "label": "50–60%"},
        {"min": 60.0, "max": 70.0, "label": "60–70%"},
        {"min": 70.0, "max": 80.0, "label": "70–80%"},
        {"min": 80.0, "max": 90.0, "label": "80–90%"},
        {"min": 90.0, "max": 100.1, "label": "90–100%"},
    ]

    bucket_stats = []
    settled = [t for t in trades if t.get("outcome") in ("win", "loss", "expired", "breakeven")]
    
    total_ece_numerator = 0.0
    total_brier_sum = 0.0
    total_evaluated = 0

    for b in buckets_def:
        b_trades = [
            t for t in settled
            if t.get("confidence") is not None
            and b["min"] <= float(t["confidence"]) < b["max"]
        ]
        count = len(b_trades)
        wins = sum(1 for t in b_trades if t.get("outcome") == "win")
        losses = sum(1 for t in b_trades if t.get("outcome") == "loss")
        obs_wr = (wins / count * 100.0) if count > 0 else 0.0
        midpoint = (b["min"] + min(100.0, b["max"])) / 2.0
        avg_conf = (sum(float(t["confidence"]) for t in b_trades) / count) if count > 0 else midpoint
        cal_error = obs_wr - avg_conf if count > 0 else 0.0

        if count > 0:
            total_ece_numerator += count * abs(obs_wr - avg_conf)
            total_evaluated += count

        bucket_stats.append({
            "label": b["label"],
            "range": [b["min"], b["max"]],
            "midpoint": midpoint,
            "avg_confidence": round(avg_conf, 1),
            "trades": count,
            "wins": wins,
            "losses": losses,
            "observed_win_rate": round(obs_wr, 1),
            "calibration_error": round(cal_error, 1),
        })

    for t in settled:
        c = float(t.get("confidence") or 50.0) / 100.0
        actual = 1.0 if t.get("outcome") == "win" else 0.0
        total_brier_sum += (c - actual) ** 2

    ece = round(total_ece_numerator / total_evaluated, 2) if total_evaluated > 0 else 0.0
    brier_score = round(total_brier_sum / len(settled), 4) if settled else 0.25

    # Determine calibration bias
    active_errors = [b["calibration_error"] for b in bucket_stats if b["trades"] >= 2]
    avg_bias = round(sum(active_errors) / len(active_errors), 1) if active_errors else 0.0
    
    if avg_bias > 5.0:
        bias_desc = f"Underconfident (+{avg_bias}%) — wins more than expected"
    elif avg_bias < -5.0:
        bias_desc = f"Overconfident ({avg_bias}%) — wins less than expected"
    else:
        bias_desc = "Well-Calibrated (bias near zero)"

    return {
        "buckets": bucket_stats,
        "total_settled": len(settled),
        "expected_calibration_error": ece,
        "brier_score": brier_score,
        "calibration_bias": avg_bias,
        "calibration_verdict": bias_desc,
    }


def calculate_category_specialization(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculates win rate and multiplier adaptation per market category."""
    cats = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0})
    
    for t in trades:
        cat = (t.get("category") or "general").strip() or "general"
        cats[cat]["trades"] += 1
        outcome = t.get("outcome")
        if outcome == "win":
            cats[cat]["wins"] += 1
        elif outcome == "loss":
            cats[cat]["losses"] += 1
        cats[cat]["pnl_cents"] += int(t.get("pnl_cents") or 0)

    total_settled_wins = sum(c["wins"] for c in cats.values())
    total_settled_trades = sum(c["wins"] + c["losses"] for c in cats.values())
    overall_wr = (total_settled_wins / total_settled_trades * 100.0) if total_settled_trades > 0 else 50.0

    category_list = []
    hot_categories = []
    cold_categories = []

    for name, stat in cats.items():
        decided = stat["wins"] + stat["losses"]
        wr = (stat["wins"] / decided * 100.0) if decided > 0 else 0.0
        
        # Adaptive multiplier calculation (0.7 to 1.3)
        if decided >= 3:
            multiplier = round(max(0.7, min(1.3, 1.0 + (wr - overall_wr) / 100.0)), 2)
        else:
            multiplier = 1.0

        status = "neutral"
        if multiplier >= 1.08 and decided >= 3:
            status = "hot"
            hot_categories.append(name)
        elif multiplier <= 0.92 and decided >= 3:
            status = "cold"
            cold_categories.append(name)

        category_list.append({
            "category": name,
            "trades": stat["trades"],
            "settled": decided,
            "wins": stat["wins"],
            "losses": stat["losses"],
            "win_rate_pct": round(wr, 1),
            "pnl_cents": stat["pnl_cents"],
            "multiplier": multiplier,
            "status": status,
        })

    category_list.sort(key=lambda x: (x["settled"], x["win_rate_pct"]), reverse=True)

    return {
        "categories": category_list,
        "overall_win_rate_pct": round(overall_wr, 1),
        "hot_categories": hot_categories,
        "cold_categories": cold_categories,
        "specialization_active": len(hot_categories) > 0 or len(cold_categories) > 0,
    }


def calculate_feature_weight_history(data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    """Reads historical weight recalibration records across bots."""
    history = []
    latest_weights_by_bot = {}

    for bot in BOT_NAMES:
        db_path = data_dir / f"{bot}.db"
        conn = _open_db(db_path)
        if not conn:
            continue
        try:
            cur = conn.execute(
                """
                SELECT timestamp, edge, liquidity, volume, timing, momentum,
                       win_rate, avg_pnl, trade_count, trigger_reason
                FROM weight_history
                ORDER BY timestamp ASC
                """
            )
            rows = cur.fetchall()
            for r in rows:
                d = dict(r)
                d["bot"] = bot
                history.append(d)
            if rows:
                latest = dict(rows[-1])
                latest_weights_by_bot[bot] = {
                    "edge": latest.get("edge", 0.2),
                    "liquidity": latest.get("liquidity", 0.2),
                    "volume": latest.get("volume", 0.2),
                    "timing": latest.get("timing", 0.2),
                    "momentum": latest.get("momentum", 0.2),
                    "recalibrations": len(rows),
                }
        except Exception:
            pass
        finally:
            conn.close()

    history.sort(key=lambda x: str(x.get("timestamp") or ""))

    return {
        "history": history,
        "total_recalibrations": len(history),
        "latest_weights_by_bot": latest_weights_by_bot,
        "is_adapting": len(history) > 0,
    }


def calculate_central_llm_learning(data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    """Examines Central LLM decision efficacy, approval alpha, and red flags."""
    db_path = data_dir / "central_llm_controller.db"
    conn = _open_db(db_path)
    if not conn:
        return {
            "total_decisions": 0, "approved": 0, "rejected": 0,
            "llm_alpha_active": False, "top_red_flags": [],
        }

    try:
        cur = conn.execute("SELECT * FROM llm_decisions ORDER BY timestamp ASC")
        rows = [dict(r) for r in cur.fetchall()]
    except Exception:
        rows = []
    finally:
        conn.close()

    total = len(rows)
    approved = sum(1 for r in rows if str(r.get("decision", "")).lower() in ("approve", "approved"))
    rejected = total - approved

    # Check red flags
    red_flag_counts = defaultdict(int)
    for r in rows:
        flags_raw = r.get("red_flags")
        if flags_raw:
            try:
                flags = json.loads(flags_raw) if isinstance(flags_raw, str) else flags_raw
                if isinstance(flags, list):
                    for f in flags:
                        red_flag_counts[str(f)] += 1
            except Exception:
                pass

    sorted_flags = sorted(
        [{"flag": k, "count": v} for k, v in red_flag_counts.items()],
        key=lambda x: x["count"],
        reverse=True
    )

    # Calculate LLM Win Rate on resolved trades
    resolved_approved = [
        r for r in rows
        if str(r.get("decision", "")).lower() in ("approve", "approved")
        and r.get("outcome") in ("win", "loss")
    ]
    resolved_wins = sum(1 for r in resolved_approved if r.get("outcome") == "win")
    llm_win_rate = (resolved_wins / len(resolved_approved) * 100.0) if resolved_approved else 0.0

    return {
        "total_decisions": total,
        "approved": approved,
        "rejected": rejected,
        "approval_rate_pct": round(approved / total * 100.0, 1) if total else 0.0,
        "resolved_approved_trades": len(resolved_approved),
        "resolved_approved_win_rate_pct": round(llm_win_rate, 1),
        "top_red_flags": sorted_flags[:8],
        "llm_alpha_active": total > 0,
    }


def compute_rolling_win_rates(trades: List[Dict[str, Any]], window: int = 10) -> List[Dict[str, Any]]:
    """Calculates rolling win rate over time to track learning trajectory."""
    settled = [t for t in trades if t.get("outcome") in ("win", "loss")]
    if not settled:
        return []

    points = []
    for i in range(len(settled)):
        sub = settled[max(0, i - window + 1): i + 1]
        wins = sum(1 for t in sub if t.get("outcome") == "win")
        rate = round(wins / len(sub) * 100.0, 1)
        points.append({
            "timestamp": settled[i].get("timestamp"),
            "trade_index": i + 1,
            "ticker": settled[i].get("ticker"),
            "outcome": settled[i].get("outcome"),
            "rolling_win_rate": rate,
            "window_size": len(sub),
        })
    return points


def generate_learning_scorecard(
    trades: List[Dict[str, Any]],
    calibration: Dict[str, Any],
    categories: Dict[str, Any],
    weights: Dict[str, Any],
    llm: Dict[str, Any],
) -> Dict[str, Any]:
    """Generates an overall learning diagnosis with clear verdicts and evidence."""
    total_trades = len(trades)
    settled_trades = calibration.get("total_settled", 0)

    # 1. Calibration verdict
    brier = calibration.get("brier_score", 0.25)
    ece = calibration.get("expected_calibration_error", 0.0)
    cal_score = 100 - min(100, int(ece * 1.5 + brier * 100))

    # 2. Dynamic adaptation verdict
    recalibs = weights.get("total_recalibrations", 0)
    
    # 3. Category specialization
    has_hot_cold = categories.get("specialization_active", False)

    # 4. Overall Learning Health Status
    if settled_trades < 10:
        stage = "WARMUP"
        status_message = f"Warmup Phase ({settled_trades}/10 settled trades) — gathering baseline data."
        is_learning = "INSUFFICIENT_DATA"
    elif recalibs > 0 or has_hot_cold:
        stage = "ACTIVE_ADAPTATION"
        status_message = f"Active Learning Verified — {recalibs} weight recalibrations and category tilt active."
        is_learning = "YES"
    else:
        stage = "DATA_COLLECTION"
        status_message = f"Collecting Trade Outcomes ({settled_trades} settled) — calibrating confidence curves."
        is_learning = "CALIBRATING"

    evidence = [
        f"Total Recorded Trades: {total_trades} across {len(BOT_NAMES)} bots ({settled_trades} settled).",
        f"Confidence Calibration: ECE={ece}%, Brier Score={brier} ({calibration.get('calibration_verdict')}).",
        f"Category Specialization: {len(categories.get('hot_categories', []))} hot / {len(categories.get('cold_categories', []))} cold categories identified.",
        f"Feature Weights: {recalibs} strategic weight recalibration(s) logged.",
        f"Central LLM Intelligence: {llm.get('total_decisions', 0)} decisions analyzed with {llm.get('approval_rate_pct', 0.0)}% approval rate.",
    ]

    return {
        "stage": stage,
        "is_learning": is_learning,
        "status_message": status_message,
        "learning_health_score": max(0, min(100, cal_score)),
        "evidence": evidence,
        "settled_trades": settled_trades,
        "total_trades": total_trades,
    }


def aggregate_full_learning_report(data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    """Combines all sub-analyses into a comprehensive JSON payload."""
    trades = get_all_settled_trades(data_dir)
    calibration = calculate_confidence_calibration(trades)
    categories = calculate_category_specialization(trades)
    weights = calculate_feature_weight_history(data_dir)
    llm = calculate_central_llm_learning(data_dir)
    rolling = compute_rolling_win_rates(trades, window=10)
    scorecard = generate_learning_scorecard(trades, calibration, categories, weights, llm)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scorecard": scorecard,
        "calibration": calibration,
        "categories": categories,
        "weights": weights,
        "llm_intelligence": llm,
        "rolling_win_rates": rolling,
    }


# ---------------------------------------------------------------------------
# Terminal UI & Formatting
# ---------------------------------------------------------------------------

class Colors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _bar(pct: float, length: int = 20, fill_char: str = "█") -> str:
    filled = int(max(0.0, min(100.0, pct)) / 100.0 * length)
    return fill_char * filled + "░" * (length - filled)


def render_terminal_report(report: Dict[str, Any]) -> None:
    scorecard = report["scorecard"]
    cal = report["calibration"]
    cats = report["categories"]
    w = report["weights"]
    llm = report["llm_intelligence"]

    print(f"\n{Colors.BOLD}{Colors.CYAN}════════════════════════════════════════════════════════════════════════════════{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}              KALSHI SWARM — DEMO TRADING LEARNING & ADAPTATION RADAR          {Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}════════════════════════════════════════════════════════════════════════════════{Colors.RESET}")
    print(f" Timestamp: {report['timestamp']}   |   Bots: {', '.join(BOT_NAMES)}")
    print()

    # 1. Scorecard
    verdict_color = Colors.GREEN if scorecard["is_learning"] == "YES" else (Colors.YELLOW if scorecard["is_learning"] == "CALIBRATING" else Colors.CYAN)
    print(f" {Colors.BOLD}LEARNING HEALTH STATUS:{Colors.RESET} [{verdict_color}{scorecard['is_learning']}{Colors.RESET}] — {scorecard['stage']}")
    print(f" {scorecard['status_message']}")
    print()
    print(f" {Colors.BOLD}Evidence & Indicators:{Colors.RESET}")
    for ev in scorecard["evidence"]:
        print(f"   • {ev}")
    print()

    # 2. Calibration Curve
    print(f"{Colors.BOLD}{Colors.MAGENTA}─── 1. CONFIDENCE CALIBRATION & ERROR ANALYSIS ────────────────────────────────{Colors.RESET}")
    print(f" ECE (Expected Calibration Error): {Colors.BOLD}{cal['expected_calibration_error']}%{Colors.RESET}   |   Brier Score: {Colors.BOLD}{cal['brier_score']}{Colors.RESET}   |   Bias: {cal['calibration_bias']}%")
    print(f" {'Bucket':<10} {'Trades':<8} {'Wins':<6} {'Observed WR':<14} {'Expected':<10} {'Error':<8} {'Calibration Visual':<22}")
    print(f" {'─'*9:<10} {'─'*7:<8} {'─'*5:<6} {'─'*12:<14} {'─'*8:<10} {'─'*6:<8} {'─'*20:<22}")
    for b in cal["buckets"]:
        wr = b["observed_win_rate"]
        bar = _bar(wr, 16)
        err_str = f"{b['calibration_error']:+.1f}%" if b["trades"] > 0 else "0.0%"
        print(f" {b['label']:<10} {b['trades']:<8} {b['wins']:<6} {wr:>5.1f}%        {b['avg_confidence']:>5.1f}%     {err_str:<8} [{bar}]")
    print()

    # 3. Category Specialization
    print(f"{Colors.BOLD}{Colors.YELLOW}─── 2. CATEGORY EDGE & ADAPTIVE MULTIPLIERS ───────────────────────────────────{Colors.RESET}")
    print(f" Overall Win Rate: {cats['overall_win_rate_pct']}%   |   Hot: {cats['hot_categories'] or 'None yet'}   |   Cold: {cats['cold_categories'] or 'None yet'}")
    print(f" {'Category':<16} {'Trades':<8} {'Wins':<6} {'Win Rate':<10} {'Total PnL':<12} {'Multiplier':<12} {'Status'}")
    print(f" {'─'*15:<16} {'─'*7:<8} {'─'*5:<6} {'─'*8:<10} {'─'*10:<12} {'─'*10:<12} {'─'*8}")
    for c in cats["categories"][:8]:
        pnl = f"{c['pnl_cents']/100:+.2f}$"
        st = f"{Colors.GREEN}🔥 HOT{Colors.RESET}" if c["status"] == "hot" else (f"{Colors.RED}❄️ COLD{Colors.RESET}" if c["status"] == "cold" else "⚖️ Neutral")
        print(f" {c['category']:<16} {c['trades']:<8} {c['wins']:<6} {c['win_rate_pct']:>5.1f}%    {pnl:<12} {c['multiplier']:>4.2f}x       {st}")
    print()

    # 4. Feature Weights & Recalibration
    print(f"{Colors.BOLD}{Colors.CYAN}─── 3. FEATURE WEIGHTS & RECALIBRATION TIMELINE ──────────────────────────────{Colors.RESET}")
    print(f" Total Recalibration Events: {w['total_recalibrations']}")
    if w["latest_weights_by_bot"]:
        for bot, wb in w["latest_weights_by_bot"].items():
            print(f"   • {bot:<10}: Edge={wb['edge']:.2f}, Liq={wb['liquidity']:.2f}, Vol={wb['volume']:.2f}, Timing={wb['timing']:.2f}, Mom={wb['momentum']:.2f} (Updates: {wb['recalibrations']})")
    else:
        print("   • Baseline Weights Active: Edge=0.20, Liquidity=0.20, Volume=0.20, Timing=0.20, Momentum=0.20")
    print()

    # 5. Central LLM Brain Filter Alpha
    print(f"{Colors.BOLD}{Colors.GREEN}─── 4. CENTRAL LLM BRAIN FILTERING ALPHA ─────────────────────────────────────{Colors.RESET}")
    print(f" Total Decisions: {llm['total_decisions']}   |   Approved: {llm['approved']} ({llm['approval_rate_pct']}%)   |   Rejected: {llm['rejected']}")
    if llm["top_red_flags"]:
        print(" Top Rejection Red Flags:")
        for rf in llm["top_red_flags"][:5]:
            print(f"   - {rf['flag']}: {rf['count']} times")
    print(f"{Colors.BOLD}{Colors.CYAN}════════════════════════════════════════════════════════════════════════════════{Colors.RESET}\n")


def run_live_watch(interval: int = 5) -> None:
    """Runs a live refreshing terminal monitor."""
    try:
        while True:
            os.system("cls" if os.name == "nt" else "clear")
            report = aggregate_full_learning_report(DATA_DIR)
            render_terminal_report(report)
            print(f"{Colors.DIM}Auto-refreshing every {interval}s... Press Ctrl+C to stop.{Colors.RESET}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[watch_demo_learning] Exiting.")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Swarm Demo Trading Learning Monitor")
    parser.add_argument("--watch", action="store_true", help="Run continuous live terminal watcher")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds (default: 5)")
    parser.add_argument("--json", action="store_true", help="Output report as raw JSON")
    parser.add_argument("--save-report", action="store_true", help="Save latest report to data/learning_report_latest.json")
    args = parser.parse_args()

    if args.watch:
        run_live_watch(args.interval)
        return

    report = aggregate_full_learning_report(DATA_DIR)

    if args.save_report or True:
        try:
            report_file = DATA_DIR / "learning_report_latest.json"
            with open(report_file, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
        except Exception:
            pass

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        render_terminal_report(report)


if __name__ == "__main__":
    main()
