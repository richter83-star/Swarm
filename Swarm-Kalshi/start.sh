#!/bin/bash
# Kalshi Swarm Launcher for Linux/VPS
# - Pulls latest code from GitHub
# - Creates venv if missing
# - Installs/updates dependencies
# - Starts the daemon (which auto-restarts on crash)

set -e
cd "$(dirname "$0")"

echo "=== Kalshi Swarm Startup $(date) ==="

# Pull latest from GitHub
echo "[1/4] Pulling latest code from GitHub..."
git pull origin main

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "[2/4] Creating virtual environment..."
    python3 -m venv venv
else
    echo "[2/4] Virtual environment exists, skipping creation."
fi

# Activate venv
source venv/bin/activate

# Install/update dependencies
echo "[3/4] Installing dependencies..."
pip install -q -r requirements.txt

# Start the daemon
echo "[4/4] Starting swarm daemon..."
python swarm_daemon.py
