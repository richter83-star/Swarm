@echo off
REM stop_dashboard.bat - Stop running dashboard server and/or local tunnel watchdog
cd /d "%~dp0"
echo =======================================================
echo Stopping Kalshi Dashboard & Local Tunnel Watchdog...
echo =======================================================

python manage_swarm.py dashboard --stop

REM Stop background tunnel watchdog processes if running
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tunnel_vps_dashboard.py*' -or ($_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*8888:127.0.0.1:8888*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('  • Terminated ' + $_.Name + ' (PID ' + $_.ProcessId + ')') }"

if exist "data\tunnel.pid" del /f /q "data\tunnel.pid" >nul 2>&1

echo [OK] All local dashboard and tunnel processes stopped.
timeout /t 3
