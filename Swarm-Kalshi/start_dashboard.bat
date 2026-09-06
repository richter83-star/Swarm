@echo off
REM start_dashboard.bat - Connect to the 24/7 VPS Kalshi Swarm Dashboard via persistent tunnel
cd /d "%~dp0"
echo =======================================================
echo Connecting to Kalshi Swarm 24/7 VPS Dashboard...
echo Local Address: http://127.0.0.1:8888
echo =======================================================

REM Start the persistent background tunnel watchdog if not already running
wscript start_tunnel_hidden.vbs

REM Small pause to ensure the tunnel socket binds
timeout /t 2 /nobreak >nul

REM Open the dashboard in default browser
start "" http://127.0.0.1:8888

echo.
echo [OK] Tunnel watchdog is running silently in the background.
echo [OK] Monitored 24/7 with automatic self-healing reconnect.
echo You can safely close this terminal window.
echo.
timeout /t 5
