# Running Swarm as Windows Service (More Stable)

## Option 1: Use NSSM (Non-Sucking Service Manager) - RECOMMENDED

1. Download NSSM from https://nssm.cc/download
2. Extract nssm.exe to C:\Windows\System32 (or any PATH location)
3. Open CMD as Administrator and run:

```cmd
nssm install KalshiSwarm
```

4. In the GUI:
   - Path: C:\Python314\python.exe (or your python path)
   - Startup directory: D:\kalshi-swarm-v4
   - Arguments: swarm_daemon.py
   - Display name: Kalshi Trading Swarm
   - Startup type: Automatic

5. Click "Install service"
6. Start the service: `net start KalshiSwarm`

## Option 2: Use Task Scheduler

1. Open Task Scheduler (taskschd.msc)
2. Create Basic Task
3. Name: Kalshi Swarm Daemon
4. Trigger: At startup + When user logs on
5. Action: Start a program
6. Program: python
7. Arguments: D:\kalshi-swarm-v4\swarm_daemon.py
8. Check "Run with highest privileges"
9. Settings tab: Check "Allow task to be run on demand"
10. Check "If the task fails, restart every: 1 minute"

## Option 3: PowerShell Script (Quick Fix)

Run this as Administrator to add real-time protection exclusions:

```powershell
# Add path exclusion
Add-MpPreference -ExclusionPath "D:\kalshi-swarm-v4"

# Add process exclusion
Add-MpPreference -ExclusionProcess "python.exe"
Add-MpPreference -ExclusionProcess "pythonw.exe"

# Verify
Get-MpPreference | Select-Object ExclusionPath, ExclusionProcess
```

## Current Workaround (Daemon)

The swarm_daemon.py already handles auto-restart, which mitigates the issue.

## Recommended Immediate Action

1. Run the add_defender_exclusions.bat as Administrator
2. OR run the PowerShell commands above
3. This should stop the SIGKILL events

## Note

The SIGKILL pattern (every ~15-20 min) matches Windows Defender's real-time scanning behavior for processes that:
- Make network requests to external APIs
- Run continuously in background
- Use file system operations

Adding exclusions tells Defender to trust these processes.
