"""
Swarm Monitor
=============
Run on-demand or via cron for a clean status report.
Logs hourly summaries and alerts on critical issues.

Usage:
    python3 monitor.py           # print report
    python3 monitor.py --log     # print + append to logs/monitor.log
    python3 monitor.py --alert   # only print if something is wrong
"""

import argparse
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
BOTS = ["sentinel", "oracle", "pulse", "vanguard"]

ALERT_BALANCE_THRESHOLD = 500   # cents — alert if below $5.00
ALERT_LOSS_STREAK = 5           # alert if any bot hits this many consecutive losses


def check_processes():
    result = subprocess.run(
        ["ps", "aux"], capture_output=True, text=True
    )
    lines = result.stdout
    procs = {
        "daemon":      "swarm_daemon.py" in lines,
        "coordinator": "run_swarm_with_ollama_brain.py" in lines,
        "sentinel":    "--bot-name sentinel" in lines,
        "oracle":      "--bot-name oracle" in lines,
        "pulse":       "--bot-name pulse" in lines,
        "vanguard":    "--bot-name vanguard" in lines,
    }
    return procs


def load_risk_state(bot):
    path = DATA_DIR / f"{bot}_risk_state.json"
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def load_live_stats(bot):
    db_path = DATA_DIR / f"{bot}.db"
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("""
            SELECT
                SUM(CASE WHEN action='buy' AND outcome='win'     THEN 1 ELSE 0 END),
                SUM(CASE WHEN action='buy' AND outcome='loss'    THEN 1 ELSE 0 END),
                SUM(CASE WHEN action='buy' AND outcome='pending' THEN 1 ELSE 0 END),
                SUM(CASE WHEN action='buy' AND outcome='breakeven' THEN 1 ELSE 0 END)
            FROM trades
        """).fetchone()
        conn.close()
        wins, losses, pending, be = [v or 0 for v in row]
        resolved = wins + losses
        win_rate = round((wins / resolved) * 100, 1) if resolved else 0.0
        return {"wins": wins, "losses": losses, "pending": pending, "breakeven": be, "win_rate": win_rate}
    except Exception:
        return None


def build_report():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    procs = check_processes()
    alerts = []
    lines = []

    lines.append("=" * 56)
    lines.append(f"  SWARM MONITOR REPORT — {now}")
    lines.append("=" * 56)

    # Process health
    lines.append("\n[ PROCESSES ]")
    for name, running in procs.items():
        status = "✅ running" if running else "❌ DOWN"
        lines.append(f"  {name:<14} {status}")
        if not running:
            alerts.append(f"CRITICAL: {name} is DOWN")

    # Per-bot stats
    lines.append("\n[ BOT STATS ]")
    lines.append(f"  {'Bot':<12} {'Balance':>9} {'Daily P&L':>10} {'Trades':>7} {'Live W/L':>10} {'WinRate':>8} {'Streak':>7} {'Open':>5}")
    lines.append("  " + "-" * 72)

    for bot in BOTS:
        risk = load_risk_state(bot)
        live = load_live_stats(bot)

        if not risk:
            lines.append(f"  {bot.capitalize():<12} {'N/A':>9}")
            alerts.append(f"WARNING: Could not read {bot} risk state")
            continue

        balance = risk.get("current_balance_cents", 0)
        peak    = risk.get("peak_balance_cents", 0)
        daily   = risk.get("daily", {})
        pnl     = daily.get("gross_pnl_cents", 0)
        trades  = daily.get("trades_today", 0)
        streak  = risk.get("consecutive_losses", 0)
        open_p  = risk.get("open_position_count", 0)
        drawdown = round((1 - balance / peak) * 100, 1) if peak else 0

        wins     = live["wins"] if live else 0
        losses   = live["losses"] if live else 0
        win_rate = live["win_rate"] if live else 0.0
        wl_str   = f"W{wins}/L{losses}"

        bal_str  = f"${balance/100:.2f}"
        pnl_str  = f"{'+' if pnl >= 0 else ''}{pnl/100:.2f}"

        lines.append(
            f"  {bot.capitalize():<12} {bal_str:>9} {pnl_str:>10} {trades:>7} {wl_str:>10} {win_rate:>7.1f}% {streak:>7} {open_p:>5}"
        )

        if balance < ALERT_BALANCE_THRESHOLD:
            alerts.append(f"WARNING: {bot} balance low (${balance/100:.2f})")
        if streak >= ALERT_LOSS_STREAK:
            alerts.append(f"WARNING: {bot} on {streak}-loss streak")
        if drawdown >= 10:
            alerts.append(f"WARNING: {bot} drawdown {drawdown}%")

    # Alerts
    if alerts:
        lines.append("\n[ ⚠️  ALERTS ]")
        for a in alerts:
            lines.append(f"  {a}")
    else:
        lines.append("\n[ ✅ NO ALERTS — all systems nominal ]")

    lines.append("=" * 56)
    return "\n".join(lines), alerts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log",   action="store_true", help="Append report to logs/monitor.log")
    parser.add_argument("--alert", action="store_true", help="Only output if there are alerts")
    args = parser.parse_args()

    report, alerts = build_report()

    if args.alert and not alerts:
        return

    print(report)

    if args.log:
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / "monitor.log"
        with open(log_path, "a") as f:
            f.write(report + "\n\n")


if __name__ == "__main__":
    main()
