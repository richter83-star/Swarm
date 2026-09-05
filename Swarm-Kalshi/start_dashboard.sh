#!/bin/bash
# start_dashboard.sh — Launch the Kalshi Swarm dashboard on port 8888
# Runs independently of the swarm. Safe to restart without affecting bots.
#
# Usage:
#   bash start_dashboard.sh
#   bash start_dashboard.sh --port 8888
#   bash start_dashboard.sh --host 0.0.0.0

cd "$(dirname "$0")"
source .env 2>/dev/null || true

PYTHON_BIN="python"
if [ -f ".venv/bin/python3" ]; then
    PYTHON_BIN=".venv/bin/python3"
elif [ -f ".venv/Scripts/python.exe" ]; then
    PYTHON_BIN=".venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
fi

echo "Starting Kalshi Swarm Dashboard on port 8888 (http://localhost:8888)..."
exec "$PYTHON_BIN" dashboard_new/server.py \
    --host 0.0.0.0 \
    --port 8888 \
    --project-root "$(pwd)" \
    "$@"
