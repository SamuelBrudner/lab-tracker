<#
.SYNOPSIS
    Enable (or update) a per-user daily-review cadence for one project.

.DESCRIPTION
    Thin wrapper over PATCH /projects/{project_id}/graph-draft-batch-settings.
    Setting another user's per-user row requires an OWNER/admin token that is
    WRITABLE (not the read-only "Scheduler trigger" level). You can equally do
    this in the web app: Batches page -> pick the project -> set the per-user
    cadence.

.EXAMPLE
    # Enable Josh's review to generate at 17:00 America/New_York
    $env:LAB_TRACKER_API_KEY = 'lpat_...writable-admin...'
    ./scripts/enable-daily-review-cadence.ps1 `
        -BaseUrl 'https://mwcppc01ysbc155.tail79f9d8.ts.net' `
        -ProjectId 'b97935f1-42c6-475d-b35f-0e23607d11e6' `
        -UserId    'b8bb5ff5-ce70-4a21-b0f5-d90f6887d85d' `
        -RunAtLocalTime '17:00' -TimeZone 'America/New_York'
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$BaseUrl,
    [Parameter(Mandatory)] [string]$ProjectId,
    [Parameter(Mandatory)] [string]$UserId,
    [string]$ApiKey          = $env:LAB_TRACKER_API_KEY,
    [string]$RunAtLocalTime  = '17:00',
    [string]$TimeZone        = 'America/New_York',
    [int]   $CadenceMinutes  = 1440,
    [bool]  $Enabled         = $true
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ApiKey)) {
    throw "Missing -ApiKey (or LAB_TRACKER_API_KEY). Needs a WRITABLE admin/owner token."
}
$BaseUrl = $BaseUrl.TrimEnd('/')

$body = @{
    enabled           = $Enabled
    cadence_minutes   = $CadenceMinutes
    run_at_local_time = $RunAtLocalTime
    timezone_name     = $TimeZone
    user_id           = $UserId
} | ConvertTo-Json

$result = Invoke-RestMethod -Method Patch `
    -Uri "$BaseUrl/projects/$ProjectId/graph-draft-batch-settings" `
    -Headers @{ Authorization = "Bearer $ApiKey" } `
    -ContentType 'application/json' `
    -Body $body -TimeoutSec 60

Write-Host "Cadence saved. next_run_at (UTC): $($result.data.next_run_at)"
$result.data | Format-List settings_id, project_id, user_id, enabled, run_at_local_time, timezone_name, next_run_at
