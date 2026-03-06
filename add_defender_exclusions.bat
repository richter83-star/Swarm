@echo off
REM Add Windows Defender exclusions for Kalshi Swarm
REM Run as Administrator

echo Adding exclusions for Kalshi Swarm...
echo.

REM Add folder exclusion
powershell -Command "Add-MpPreference -ExclusionPath 'D:\kalshi-swarm-v4'"
echo [OK] Added folder exclusion: D:\kalshi-swarm-v4

REM Add process exclusions
powershell -Command "Add-MpPreference -ExclusionProcess 'python.exe'"
echo [OK] Added process exclusion: python.exe

powershell -Command "Add-MpPreference -ExclusionProcess 'pythonw.exe'"
echo [OK] Added process exclusion: pythonw.exe

echo.
echo Exclusions added. Windows Defender will ignore these processes.
echo.
echo To verify exclusions, run:
echo   Get-MpPreference | Select-Object -Property ExclusionPath, ExclusionProcess
pause
