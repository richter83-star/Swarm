@echo off
:: Kalshi Swarm Launcher — used by Windows Task Scheduler
:: Starts swarm_daemon.py in the background (hidden window via pythonw)

cd /d "%~dp0"

:: Use pythonw so no console window is shown when running at boot
pythonw swarm_daemon.py

:: If pythonw is unavailable fall back to python with minimized window
if %errorlevel% neq 0 (
    start /min python swarm_daemon.py
)
