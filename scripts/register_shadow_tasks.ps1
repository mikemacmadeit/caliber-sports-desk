# Register restart-safe Caliber shadow jobs in Windows Task Scheduler.
# Does not enable Validated Play. Idempotent.
param(
  [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$wrapper = Join-Path $PSScriptRoot "shadow_schedule.ps1"
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $wrapper)) { throw "Missing $wrapper" }
if (-not (Test-Path $python)) { throw "Missing venv python at $python" }

$tasks = @(
  @{ Name = "CaliberShadowOdds";        Job = "odds";          Minutes = 5;   Description = "Caliber odds refresh every few minutes; near-kickoff prices stay fresh" },
  @{ Name = "CaliberShadowInjuries";    Job = "injuries";      Minutes = 60;  Description = "Caliber nflverse injury refresh" },
  @{ Name = "CaliberShadowFbsInjuries"; Job = "fbs-injuries";  Minutes = 60;  Description = "Caliber API-Sports FBS injury refresh" },
  @{ Name = "CaliberShadowInactives";   Job = "inactives";     Minutes = 10;  Description = "Caliber ESPN inactive lists inside the NFL kickoff window" },
  @{ Name = "CaliberShadowPortal";      Job = "portal";        Minutes = 720; Description = "Caliber CFBD transfer portal roster moves" },
  @{ Name = "CaliberShadowCycle";       Job = "cycle";         Minutes = 60;  Description = "Caliber shadow cycle: injuries, odds, forecasts, capture, settle, report" },
  @{ Name = "CaliberShadowSettle";      Job = "settle";        Minutes = 30;  Description = "Caliber shadow settlement for finished games" },
  @{ Name = "CaliberShadowReport";      Job = "report";        Minutes = 120; Description = "Caliber shadow report refresh" }
)

foreach ($task in $tasks) {
  $existing = Get-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
  if ($existing -and $existing.State -eq "Running") {
    Stop-ScheduledTask -TaskName $task.Name -ErrorAction SilentlyContinue
  }
  Unregister-ScheduledTask -TaskName $task.Name -Confirm:$false -ErrorAction SilentlyContinue
}

if ($Unregister) {
  Write-Host "Unregistered Caliber shadow tasks."
  exit 0
}

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$launcher = Join-Path $PSScriptRoot "shadow_schedule_hidden.vbs"
if (-not (Test-Path $launcher)) { throw "Missing $launcher" }

$offset = 0
foreach ($task in $tasks) {
  $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B //Nologo `"$launcher`" $($task.Job)" -WorkingDirectory $root
  # 9:00 AM through 10:00 PM local time. A few minutes of stagger keeps the jobs from writing at once.
  $now = Get-Date
  $open = $now.Date.AddHours(9).AddMinutes($offset)
  $close = $now.Date.AddHours(22)
  $triggers = @()
  if ($now -ge $open -and $now -lt $close) {
    $soon = $now.AddMinutes($offset + 1)
    $remaining = $close - $soon
    if ($remaining.TotalMinutes -ge $task.Minutes) {
      $today = New-ScheduledTaskTrigger -Once -At $soon -RepetitionInterval (New-TimeSpan -Minutes $task.Minutes) -RepetitionDuration $remaining
      $today.Repetition.StopAtDurationEnd = $false
      $triggers += $today
    }
    $open = $open.AddDays(1)
  } elseif ($now -ge $close) {
    $open = $open.AddDays(1)
  }
  $daily = New-ScheduledTaskTrigger -Daily -At $open
  $pattern = New-ScheduledTaskTrigger -Once -At $open -RepetitionInterval (New-TimeSpan -Minutes $task.Minutes) -RepetitionDuration (New-TimeSpan -Hours 13)
  $daily.Repetition = $pattern.Repetition
  $daily.Repetition.StopAtDurationEnd = $false
  $triggers = @($daily) + $triggers
  Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Description $task.Description -Force | Out-Null
  Write-Host "Registered $($task.Name) every $($task.Minutes) minutes, 9:00 AM–10:00 PM, no window — $($task.Description)"
  $offset += 2
}

Write-Host ""
Get-ScheduledTask -TaskName "CaliberShadow*" | Format-Table TaskName, State -AutoSize
Write-Host "Play remains disabled. Jobs recover after reboot via Task Scheduler."
