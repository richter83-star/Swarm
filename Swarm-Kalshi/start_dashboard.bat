@echo off
REM start_dashboard.bat - Connect to the 24/7 VPS Kalshi Swarm Dashboard via secure tunnel
cd /d "%~dp0"
echo =======================================================
echo Connecting to Kalshi Swarm 24/7 VPS Dashboard...
echo URL: http://localhost:8888
echo =======================================================
start "" http://localhost:8888
python scripts\tunnel_vps_dashboard.py
