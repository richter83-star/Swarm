#!/bin/bash
set -a
source /root/Swarm/Swarm-Kalshi/.env
set +a
cd /root/Swarm/Swarm-Kalshi
exec .venv/bin/python3 swarm_daemon.py
