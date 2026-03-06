#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the Kalshi Swarm as a Windows Task Scheduler task so it
    starts automatically at system boot and runs 24/7.

.DESCRIPTION
    Creates a task named "KalshiSwarm" that:
      - Triggers at system startup (not just logon — works even when
        the desktop is locked or no user is logged in).
      - Runs as the CURRENT USER with highest privileges.
      - Restarts automatically every 5 minutes if it stops for any reason.
      - Never expires.
      - Logs its own activity to logs\daemon.log via swarm_daemon.py.

.USAGE
    Right-click setup_autostart.ps1 → Run as Administrator
    OR from an elevated PowerShell:
        Set-ExecutionPolicy Bypass -Scope Process -Force
        .\setup_autostart.ps1

.NOTES
    Run this script ONCE to install.  Re-run to update the task.
    To remove the task: schtasks /delete /tn "KalshiSwarm" /f
#>

$TaskName    = "KalshiSwarm"
$ProjectDir  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Python      = (Get-Command python -ErrorAction SilentlyContinue).Source
$PythonW     = Join-Path (Split-Path $Python -Parent) "pythonw.exe"
$DaemonScript = Join-Path $ProjectDir "swarm_daemon.py"

# ── Validate paths ────────────────────────────────────────────────────────────
if (-not $Python) {
    Write-Error "Python not found in PATH. Install Python and re-run."
    exit 1
}
if (-not (Test-Path $DaemonScript)) {
    Write-Error "swarm_daemon.py not found at: $DaemonScript"
    exit 1
}

# Use pythonw if available (no console window), else python
$Executable = if (Test-Path $PythonW) { $PythonW } else { $Python }

Write-Host ""
Write-Host "=== Kalshi Swarm — Autostart Setup ===" -ForegroundColor Cyan
Write-Host "  Task name  : $TaskName"
Write-Host "  Executable : $Executable"
Write-Host "  Script     : $DaemonScript"
Write-Host "  Working dir: $ProjectDir"
Write-Host ""

# ── Remove any existing task ──────────────────────────────────────────────────
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ── Build task components ─────────────────────────────────────────────────────

# Action: run pythonw swarm_daemon.py from the project directory
$Action = New-ScheduledTaskAction `
    -Execute $Executable `
    -Argument "`"$DaemonScript`"" `
    -WorkingDirectory $ProjectDir

# Trigger: at system startup with a 30-second delay (gives network time to come up)
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Trigger.Delay = "PT30S"   # ISO 8601 duration: 30 seconds

# Settings: run indefinitely, restart on failure
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Days 0) `
    -RestartCount 9999 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

# Principal: run as current user with highest available privileges
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType S4U `
    -RunLevel Highest

# ── Register the task ─────────────────────────────────────────────────────────
Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $Action `
    -Trigger    $Trigger `
    -Settings   $Settings `
    -Principal  $Principal `
    -Description "Kalshi trading swarm — LLM brain + 24/7 daemon" `
    -Force | Out-Null

Write-Host "Task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host ""
Write-Host "The swarm will start automatically on next boot."
Write-Host "To start it NOW without rebooting, run:" -ForegroundColor Yellow
Write-Host "    Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Check status : Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Stop swarm   : Stop-ScheduledTask  -TaskName '$TaskName'"
Write-Host "  Remove task  : Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
Write-Host "  View logs    : Get-Content '$ProjectDir\logs\daemon.log' -Tail 50 -Wait"
Write-Host ""
