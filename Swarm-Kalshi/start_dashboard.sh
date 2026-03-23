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

echo "Starting Kalshi Swarm Dashboard on port 8888..."
exec .venv/bin/python3 dashboard_new/server.py \
    --host 0.0.0.0 \
    --port 8888 \
    --project-root "$(pwd)" \
    "$@"
