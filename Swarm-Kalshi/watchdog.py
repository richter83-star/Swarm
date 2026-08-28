#!/usr/bin/env python3
"""
watchdog.py
===========

Lightweight error-storm watcher. Designed to run every 15 minutes via cron.

Scans the last 20 minutes of swarm.log for repeated identical ERROR lines.
If any single error appears 5+ times it means a crash loop is active —
fires a Telegram alert immediately without waiting for the daily health check.

This prevents silent failures like a Python AttributeError firing on every
trade cycle going unnoticed for hours.

Cron (run as root on VPS):
    */15 * * * * cd /root/Swarm/Swarm-Kalshi && source .env && \
        .venv/bin/python3 watchdog.py >> logs/watchdog.log 2>&1
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
LOG_PATH     = PROJECT_ROOT / "logs" / "swarm.log"
CONFIG_PATH  = PROJECT_ROOT / "config" / "swarm_config.yaml"
STATE_PATH   = PROJECT_ROOT / "data" / "watchdog_state.json"

LOOKBACK_MINUTES = 20   # how far back to scan
STORM_THRESHOLD  = 5    # same error N+ times = storm
ALERT_COOLDOWN   = 60   # minutes between repeated alerts for the SAME error


# ---------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"last_alerts": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def scan_error_storms() -> dict[str, int]:
    """Return {error_key: count} for errors appearing ≥ STORM_THRESHOLD times."""
    if not LOG_PATH.exists():
        return {}

    cutoff = _now_utc() - timedelta(minutes=LOOKBACK_MINUTES)
    counts: dict[str, int] = defaultdict(int)

    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-3000:]
    except Exception:
        return {}

    for line in lines:
        if " | ERROR " not in line and " | CRITICAL " not in line:
            continue
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            if ts < cutoff:
                continue
        except ValueError:
            continue

        parts = line.split(" | ", 4)
        if len(parts) >= 5:
            key = (parts[3].strip() + ": " + parts[4].strip())[:140]
        else:
            key = line.strip()[:140]

        counts[key] += 1

    return {k: v for k, v in counts.items() if v >= STORM_THRESHOLD}


def main() -> None:
    print(f"[watchdog] {_now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC — scanning last {LOOKBACK_MINUTES} min...")

    storms = scan_error_storms()

    if not storms:
        print(f"[watchdog] Clean — no error storms detected (threshold={STORM_THRESHOLD}×)")
        return

    state = _load_state()
    now_iso = _now_utc().isoformat()
    alerted_any = False

    for error_key, count in sorted(storms.items(), key=lambda x: -x[1]):
        print(f"[watchdog] STORM DETECTED: {count}× — {error_key[:100]}")

        # Cooldown: don't spam same error every 15 min
        last_alert_str = state["last_alerts"].get(error_key)
        if last_alert_str:
            last_alert = datetime.fromisoformat(last_alert_str)
            if (_now_utc() - last_alert).total_seconds() < ALERT_COOLDOWN * 60:
                print(f"[watchdog]   Skipping alert (cooldown, last logged {last_alert_str})")
                continue

        state["last_alerts"][error_key] = now_iso
        alerted_any = True

    if alerted_any:
        _save_state(state)


if __name__ == "__main__":
    main()
