#!/usr/bin/env python3
"""
manage_swarm.py
===============

Unified CLI management console for the Kalshi Bot Swarm.

Commands:
    python manage_swarm.py start              # Start swarm in background
    python manage_swarm.py stop               # Stop all swarm & bot processes
    python manage_swarm.py restart            # Restart swarm
    python manage_swarm.py status             # Show live status & process info
    python manage_swarm.py mode [demo|live]   # View or switch trading mode (demo vs live)
    python manage_swarm.py health             # Run full 15-point health check
    python manage_swarm.py radar              # Run learning & calibration radar
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "swarm_config.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

# Ensure .env is loaded
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip("'\"")


def _read_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[ERROR] Could not read {CONFIG_PATH}: {e}")
        return {}


def _write_config(cfg: Dict[str, Any]) -> bool:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        print(f"[ERROR] Could not write {CONFIG_PATH}: {e}")
        return False


def find_swarm_processes() -> List[Dict[str, Any]]:
    """Locate all running swarm, bot_runner, or brain processes."""
    found = []
    target_scripts = (
        "run_swarm.py",
        "run_swarm_with_brain.py",
        "run_swarm_with_ollama_brain.py",
        "swarm_daemon.py",
        "bot_runner.py",
    )

    if os.name == "nt":
        # Windows via wmic / powershell
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' } | Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if res.stdout.strip():
                try:
                    data = json.loads(res.stdout.strip())
                    if isinstance(data, dict):
                        data = [data]
                    for proc in data:
                        cmdline = proc.get("CommandLine") or ""
                        pid = proc.get("ProcessId")
                        if any(t in cmdline for t in target_scripts) and pid != os.getpid():
                            found.append({
                                "pid": pid,
                                "cmd": cmdline,
                                "script": next(t for t in target_scripts if t in cmdline),
                            })
                except Exception:
                    pass
        except Exception:
            pass
    else:
        # Linux / Unix via pgrep / ps
        try:
            res = subprocess.run(["ps", "-ef"], capture_output=True, text=True, timeout=5)
            for line in res.stdout.splitlines():
                if any(t in line for t in target_scripts) and "grep" not in line and str(os.getpid()) not in line:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        found.append({
                            "pid": int(parts[1]),
                            "cmd": line,
                            "script": next(t for t in target_scripts if t in line),
                        })
        except Exception:
            pass

    return found


def start_swarm(daemon: bool = True) -> int:
    """Start the swarm with Gemini Central LLM Brain."""
    running = find_swarm_processes()
    if running:
        print(f"[INFO] Swarm is ALREADY running (Found {len(running)} processes: PIDs {[p['pid'] for p in running]}).")
        return 0

    mode = get_mode()
    cfg = _read_config()
    provider = cfg.get("central_llm", {}).get("provider", "gemini")
    model = cfg.get("central_llm", {}).get("gemini_model", "gemini-2.5-flash")

    print(f"\n[START] Launching Kalshi Swarm...")
    print(f"  • Mode:      {mode.upper()}")
    print(f"  • AI Brain:  {provider.upper()} ({model})")
    print(f"  • Log file:  {PROJECT_ROOT / 'logs' / 'swarm.log'}")

    run_script = PROJECT_ROOT / "run_swarm_with_brain.py"
    if not run_script.exists():
        run_script = PROJECT_ROOT / "run_swarm.py"

    log_file = (LOGS_DIR / "swarm.log").open("a", encoding="utf-8")

    if os.name == "nt":
        # Windows background launch
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
        # Unix background launch
        proc = subprocess.Popen(
            [sys.executable, str(run_script)],
            cwd=str(PROJECT_ROOT),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
        )

    time.sleep(2)
    new_procs = find_swarm_processes()
    if new_procs or proc.poll() is None:
        pids = [p["pid"] for p in new_procs] or [proc.pid]
        print(f"[OK] Swarm successfully started in background! PID(s): {pids}")
        return 0
    else:
        print("[ERROR] Failed to start swarm. Check logs/swarm.log for details.")
        return 1


def stop_swarm() -> int:
    """Stop all running swarm processes."""
    running = find_swarm_processes()
    if not running:
        print("[INFO] No active swarm processes found.")
        return 0

    print(f"[STOP] Stopping {len(running)} swarm processes (PIDs {[p['pid'] for p in running]})...")

    # Send kill signal file first for graceful exit
    try:
        (DATA_DIR / "kill_signal.json").write_text(
            json.dumps({"action": "kill", "timestamp": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass

    for p in running:
        pid = p["pid"]
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as e:
            print(f"  • Warning killing PID {pid}: {e}")

    time.sleep(1.5)
    remaining = find_swarm_processes()
    if not remaining:
        print("[OK] All swarm processes stopped successfully.")
        return 0
    else:
        # Force kill remaining
        for p in remaining:
            if os.name != "nt":
                try:
                    os.kill(p["pid"], signal.SIGKILL)
                except Exception:
                    pass
        print(f"[OK] Cleanup complete.")
        return 0


def restart_swarm() -> int:
    """Restart the swarm."""
    print("\n[RESTART] Restarting Kalshi Swarm...")
    stop_swarm()
    time.sleep(1)
    return start_swarm()


def get_mode() -> str:
    """Get current trading mode: 'demo' or 'live'."""
    cfg = _read_config()
    is_demo = bool(cfg.get("api", {}).get("demo_mode", True))
    return "demo" if is_demo else "live"


def set_mode(new_mode: str) -> bool:
    """Switch trading mode to 'demo' or 'live'."""
    new_mode = new_mode.strip().lower()
    if new_mode not in ("demo", "live"):
        print("[ERROR] Invalid mode. Choose 'demo' or 'live'.")
        return False

    cfg = _read_config()
    if "api" not in cfg or not isinstance(cfg["api"], dict):
        cfg["api"] = {}

    is_demo = (new_mode == "demo")
    cfg["api"]["demo_mode"] = is_demo

    if _write_config(cfg):
        print(f"\n[OK] Trading mode updated to: {new_mode.upper()} (api.demo_mode={is_demo})")
        running = find_swarm_processes()
        if running:
            print("[NOTE] Swarm is currently running. Restart the swarm for the new mode to take effect:")
            print("       python manage_swarm.py restart")
        return True
    return False


def print_status() -> None:
    """Print full console status report."""
    running = find_swarm_processes()
    mode = get_mode()
    cfg = _read_config()
    central = cfg.get("central_llm", {})
    provider = central.get("provider", "gemini")
    model = central.get("gemini_model", "gemini-2.5-flash")

    print("\n" + "=" * 60)
    print("           KALSHI SWARM COMMAND CONSOLE STATUS           ")
    print("=" * 60)
    print(f" Swarm Status   : {'🟢 RUNNING' if running else '🔴 STOPPED'}")
    print(f" Trading Mode   : {'🟡 DEMO (Simulation)' if mode == 'demo' else '🔴 LIVE CAPITAL'}")
    print(f" AI Brain       : 🔷 {provider.upper()} ({model})")
    print(f" Search Ground  : {'✅ Google Search Enabled' if central.get('gemini_search_grounding', True) else '❌ Off'}")
    print(f" Config File    : {CONFIG_PATH}")
    print("-" * 60)

    if running:
        print(" Active Processes:")
        for p in running:
            print(f"   • PID {p['pid']:<7} [{p['script']}]")
    else:
        print(" Active Processes: None")

    print("-" * 60)
    # Check bot status files
    print(" Bot Statuses:")
    for bot in ("sentinel", "oracle", "pulse", "vanguard"):
        status_file = DATA_DIR / f"{bot}_status.json"
        risk_file = DATA_DIR / f"{bot}_risk_state.json"
        st = "IDLE"
        pnl = "$0.00"
        if status_file.exists():
            try:
                sdata = json.loads(status_file.read_text(encoding="utf-8"))
                st = sdata.get("status", "ACTIVE").upper()
            except Exception:
                pass
        if risk_file.exists():
            try:
                rdata = json.loads(risk_file.read_text(encoding="utf-8"))
                cents = rdata.get("daily_pnl_cents", 0) or 0
                pnl = f"{cents/100:+.2f}$"
            except Exception:
                pass
        print(f"   • {bot:<10}: {st:<10} | Today PnL: {pnl}")

    print("=" * 60 + "\n")


def run_health() -> None:
    """Run health check."""
    subprocess.run([sys.executable, str(PROJECT_ROOT / "health_check.py")])


def run_radar() -> None:
    """Run demo learning radar."""
    subprocess.run([sys.executable, str(PROJECT_ROOT / "watch_demo_learning.py"), "--save-report"])


def main():
    parser = argparse.ArgumentParser(description="Kalshi Swarm CLI Management Console")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    subparsers.add_parser("start", help="Start the swarm in background")
    subparsers.add_parser("stop", help="Stop all swarm processes")
    subparsers.add_parser("restart", help="Restart the swarm")
    subparsers.add_parser("status", help="Show live status and process details")
    subparsers.add_parser("health", help="Run 15-point health check")
    subparsers.add_parser("radar", help="Run demo learning and calibration radar")

    mode_parser = subparsers.add_parser("mode", help="View or switch trading mode")
    mode_parser.add_argument("new_mode", nargs="?", choices=["demo", "live"], help="Set mode to 'demo' or 'live'")

    args = parser.parse_args()

    if not args.command or args.command == "status":
        print_status()
    elif args.command == "start":
        start_swarm()
    elif args.command == "stop":
        stop_swarm()
    elif args.command == "restart":
        restart_swarm()
    elif args.command == "mode":
        if args.new_mode:
            set_mode(args.new_mode)
        else:
            print(f"\nCurrent Trading Mode: {get_mode().upper()}\nTo change: python manage_swarm.py mode [demo|live]\n")
    elif args.command == "health":
        run_health()
    elif args.command == "radar":
        run_radar()


if __name__ == "__main__":
    main()
