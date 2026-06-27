<#
.SYNOPSIS
  Trigger one pass of Lab Tracker's daily review.

.DESCRIPTION
  POSTs /batches/run-due, which drafts over staged notes for every project whose
  batch settings are enabled and due. This only ever proposes a draft for human
  review -- it never commits anything to your graph.

  Auth is only needed on deployments where authentication is enabled; for a
  local runtime (auth disabled) no credentials are required. Prefer an API key
  for scheduled automations; username/password login remains supported.
#>
param(
    [string]$BaseUrl = $(if ($env:LAB_TRACKER_BASE_URL) { $env:LAB_TRACKER_BASE_URL } else { "http://127.0.0.1:8000" }),
    [string]$ApiKey = $env:LAB_TRACKER_API_KEY,
    [string]$AdminUser = $env:LAB_TRACKER_ADMIN_USER,
    [string]$AdminPass = $env:LAB_TRACKER_ADMIN_PASS
)

$ErrorActionPreference = "Stop"

$headers = @{}
if ($ApiKey) {
    $headers["Authorization"] = "Bearer $ApiKey"
} elseif ($AdminUser) {
    # Mint a fresh short-lived admin token each run (tokens expire).
    $body = @{ username = $AdminUser; password = $AdminPass } | ConvertTo-Json
    $login = Invoke-RestMethod -Method Post -Uri "$BaseUrl/auth/login" -ContentType 'application/json' -Body $body
    $headers["Authorization"] = "Bearer $($login.data.access_token)"
}

Invoke-RestMethod -Method Post -Uri "$BaseUrl/batches/run-due" -Headers $headers | Out-Null
Write-Host "lab-tracker: daily review run-due triggered ($BaseUrl)"
