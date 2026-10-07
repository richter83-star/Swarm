<#
.SYNOPSIS
    WHALE-OS control: start / stop / restart / status / calibrate / report / logs.

.DESCRIPTION
    Runs the WHALE-OS shadow runner (python -m consensus.shadow) and the HUD
    (python -m consensus.hud) as hidden background processes, tracked by PID
    files in data\whale_os\run and logging to data\whale_os\logs.

    SHADOW MODE ONLY. Nothing here places orders; the runner reads public
    Kalshi / Open-Meteo / Coinbase data and records hypothetical decisions.

.USAGE
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 start
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 status
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 stop
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 restart
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 calibrate   # rebuild HISTORY table (~25 min, background)
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 report
    powershell -ExecutionPolicy Bypass -File scripts\whale_os.ps1 logs
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "calibrate", "report", "logs")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$Root    = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
$DataDir = Join-Path $Root "data\whale_os"
$RunDir  = Join-Path $DataDir "run"
$LogDir  = Join-Path $DataDir "logs"
New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { Write-Error "python not found on PATH"; exit 1 }

$Services = [ordered]@{
    shadow = @("-m", "consensus.shadow")
    hud    = @("-m", "consensus.hud")
}

function Get-Tracked([string]$Name) {
    # Returns the live process for a PID file, or $null. Verifies the command line so a
    # recycled PID belonging to some other program is never touched.
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $pidFile)) { return $null }
    $procId = [int](Get-Content $pidFile -Raw).Trim()
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if ($p -and $p.CommandLine -match "consensus\.(shadow|hud|calibration)") { return $p }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    return $null
}

function Start-Tracked([string]$Name, [string[]]$PyArgs) {
    $existing = Get-Tracked $Name
    if ($existing) { Write-Host ("{0,-10} already running (pid {1})" -f $Name, $existing.ProcessId); return }
    $out = Join-Path $LogDir "$Name.out.log"
    $err = Join-Path $LogDir "$Name.log"
    foreach ($f in @($out, $err)) {               # keep one previous log per service
        if (Test-Path $f) { Move-Item $f "$f.1" -Force }
    }
    $p = Start-Process -FilePath $Python -ArgumentList $PyArgs -WorkingDirectory $Root `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $p.Id
    Write-Host ("{0,-10} started (pid {1})  log {2}" -f $Name, $p.Id, $err)
}

function Stop-Tracked([string]$Name) {
    $p = Get-Tracked $Name
    if (-not $p) { Write-Host ("{0,-10} not running" -f $Name); return }
    Stop-Process -Id $p.ProcessId -Force
    Remove-Item (Join-Path $RunDir "$Name.pid") -Force -ErrorAction SilentlyContinue
    Write-Host ("{0,-10} stopped (pid {1})" -f $Name, $p.ProcessId)
}

function Show-Status {
    foreach ($name in @($Services.Keys) + "calibration") {
        $p = Get-Tracked $name
        if ($p) { Write-Host ("{0,-12} RUNNING  pid {1}" -f $name, $p.ProcessId) }
        elseif ($name -ne "calibration") { Write-Host ("{0,-12} stopped" -f $name) }
    }
    try {
        $h = Invoke-WebRequest "http://127.0.0.1:8890/healthz" -UseBasicParsing -TimeoutSec 3
        Write-Host "hud          http://127.0.0.1:8890  ($($h.StatusCode))"
    } catch { Write-Host "hud          not answering on 127.0.0.1:8890" }
    Write-Host ""
    & $Python -m consensus.report 2>$null
}

Push-Location $Root
try {
    switch ($Action) {
        "start"   { foreach ($k in $Services.Keys) { Start-Tracked $k $Services[$k] } }
        "stop"    { foreach ($k in $Services.Keys) { Stop-Tracked $k } }
        "restart" { foreach ($k in $Services.Keys) { Stop-Tracked $k }; Start-Sleep 2
                    foreach ($k in $Services.Keys) { Start-Tracked $k $Services[$k] } }
        "status"  { Show-Status }
        "report"  { & $Python -m consensus.report }
        "calibrate" {
            # The runner hot-reloads the table when the file changes; no restart needed.
            # Series come from config\whale_os.yaml.
            Start-Tracked "calibration" @("-m", "consensus.calibration")
        }
        "logs"    { foreach ($k in @($Services.Keys) + "calibration") {
                        $f = Join-Path $LogDir "$k.log"
                        if (Test-Path $f) { Write-Host "== $k =="; Get-Content $f -Tail 15 } } }
    }
} finally { Pop-Location }
