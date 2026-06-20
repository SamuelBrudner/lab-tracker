<#
.SYNOPSIS
  Install a Windows Scheduled Task that runs Lab Tracker's daily review.

.DESCRIPTION
  This is the one-command setup. It registers a task that polls
  /batches/run-due every N minutes, so each project fires at its own configured
  local time. Polling is cheap and idempotent -- off-time polls find nothing due.

  Re-running updates the existing task. The default targets a local Lab Tracker
  with auth disabled, so no credentials are needed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/install-daily-review.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/install-daily-review.ps1 -IntervalMinutes 30 -BaseUrl https://lab.example.org
#>
param(
    [int]$IntervalMinutes = 15,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$TaskName = "LabTrackerDailyReview"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$trigger = Join-Path $scriptDir "daily-review-run-due.ps1"
if (-not (Test-Path $trigger)) {
    throw "Trigger script not found: $trigger"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$trigger`" -BaseUrl `"$BaseUrl`""

# Repeat every N minutes, effectively forever.
$taskTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $taskTrigger -Settings $settings `
    -Description "Polls Lab Tracker /batches/run-due so the daily review fires on each project's configured cadence." | Out-Null

Write-Host "Installed scheduled task '$TaskName' (every $IntervalMinutes min -> $BaseUrl/batches/run-due)."
Write-Host ""
Write-Host "Next: enable the daily review per project on the Batches page (/app/batches)."
Write-Host "Remove with: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
