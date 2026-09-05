@echo off
REM stop_dashboard.bat - Stop running background dashboard server
cd /d "%~dp0"
python manage_swarm.py dashboard --stop
