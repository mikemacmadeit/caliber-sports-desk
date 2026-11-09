# Prospective shadow jobs. Restart-safe. Does not enable Validated Play.
# Example Task Scheduler action:
#   powershell -File "C:\dev\Sports bet\scripts\shadow_schedule.ps1" -Job cycle
param(
  [ValidateSet("odds", "injuries", "fbs-injuries", "inactives", "portal", "capture", "settle", "cycle", "report", "ats")]
  [string]$Job = "cycle"
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
# Scheduled runs set CALIBER_SCHEDULED so Python does not open a console window.
$python = Join-Path $PWD ".venv\Scripts\python.exe"
if ($env:CALIBER_SCHEDULED -eq "1") {
  $windowless = Join-Path $PWD ".venv\Scripts\pythonw.exe"
  if (Test-Path $windowless) { $python = $windowless }
}
$worker = Join-Path $PWD "services\worker\worker\main.py"
$year = (Get-Date).Year
$env:PYTHONPATH = "packages\core"

function Test-WorkerRunning([string]$Command) {
  $hits = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match 'python' -and $_.CommandLine -like "*$Command*"
  }
  return [bool]$hits
}

# pythonw is a windowless program. A normal call returns immediately, so the
# next five-minute trigger starts a second price pull. Wait until it finishes.
function Invoke-CaliberWorker {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$WorkerArgs)
  if ($env:CALIBER_SCHEDULED -eq "1") {
    $proc = Start-Process -FilePath $python -ArgumentList (@($worker) + $WorkerArgs) -WorkingDirectory $PWD -WindowStyle Hidden -Wait -PassThru
    if ($null -ne $proc.ExitCode -and $proc.ExitCode -ne 0) { exit $proc.ExitCode }
    return
  }
  & $python $worker @WorkerArgs
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Job) {
  "odds" {
    # Shared Odds API path (Agent 3 budget). Props first, then spreads-only adapter.
    # market=spreads; near/mid/far gating via spendable_for_bucket (near may use remaining;
    # mid/far leave near_reserve untouched). Not an independent poller.
    # A pull already in flight keeps going. Do not start a second one.
    if (Test-WorkerRunning "ingest-odds-live") { exit 0 }
    Invoke-CaliberWorker ingest-odds-live
    Invoke-CaliberWorker ingest-odds-spreads --max-events 12
  }
  "injuries" { Invoke-CaliberWorker refresh-injuries --year $year }
  "fbs-injuries" { Invoke-CaliberWorker refresh-fbs-injuries --year $year }
  "inactives" { Invoke-CaliberWorker refresh-inactives }
  "portal" {
    Invoke-CaliberWorker ingest-portal --year $year
    Invoke-CaliberWorker ingest-portal --year ($year - 1)
  }
  "capture" {
    Invoke-CaliberWorker publish-model-forecasts
    Invoke-CaliberWorker shadow-capture
    # Spreads may already be refreshed by odds job; skip duplicate props spend.
    Invoke-CaliberWorker ats-cycle --skip-odds
  }
  "settle" {
    Invoke-CaliberWorker shadow-settle
    Invoke-CaliberWorker ats-shadow-settle
  }
  "report" { Invoke-CaliberWorker shadow-cycle --skip-odds --skip-publish }
  "ats" {
    # Spreads-only refresh through shared adapter/budget, then capture/settle.
    Invoke-CaliberWorker ingest-odds-spreads --max-events 12
    Invoke-CaliberWorker ats-cycle --skip-odds
  }
  "cycle" {
    Invoke-CaliberWorker refresh-injuries --year $year
    Invoke-CaliberWorker refresh-fbs-injuries --year $year
    Invoke-CaliberWorker refresh-inactives
    Invoke-CaliberWorker shadow-cycle
    # shadow-cycle may include props odds; request spreads once more via shared adapter
    # (dedupe + spendable gates prevent uncontrolled polling).
    Invoke-CaliberWorker ingest-odds-spreads --max-events 12
    Invoke-CaliberWorker ats-cycle --skip-odds
  }
}
