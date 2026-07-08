<#
.SYNOPSIS
    Register a Windows Scheduled Task that triggers due daily reviews and emails
    a contentless cue to a reviewer, every N minutes.

.DESCRIPTION
    Wraps scripts/daily-review-notify.ps1 in a repeating Scheduled Task, mirroring
    scripts/install-daily-review.ps1. The task runs as the current user and
    inherits that user's environment, so set the LAB_TRACKER_* settings the notify
    script needs (BASE_URL, API_KEY, REVIEW_USER_ID, REVIEW_EMAIL, MAIL_FROM,
    SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD) as USER environment
    variables first (e.g. via `setx` or System Settings), then run this once.

    Re-running updates the existing task. Remove with:
        Unregister-ScheduledTask -TaskName LabTrackerDailyReviewCue -Confirm:$false

.EXAMPLE
    ./scripts/install-daily-review-cue-task.ps1 -IntervalMinutes 15
#>

[CmdletBinding()]
param(
    [int]$IntervalMinutes = 15,
    [string]$TaskName = "LabTrackerDailyReviewCue"
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$notify = Join-Path $repoRoot 'scripts/daily-review-notify.ps1'
if (-not (Test-Path $notify)) { throw "Cannot find $notify" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$notify`""

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Trigger due Lab Tracker daily reviews and email a contentless cue every $IntervalMinutes min." | Out-Null

Write-Host "Registered task '$TaskName' (every $IntervalMinutes min)."
Write-Host "Dry run to verify config without sending:"
Write-Host "    ./scripts/daily-review-notify.ps1 -WhatIf"
