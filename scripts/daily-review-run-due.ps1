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

  Credentials come from -ApiKey / -AdminUser / -AdminPass (defaulting to the
  matching LAB_TRACKER_* environment variables); any left empty are read from
  -SecretsFile, the private JSON file written by install-daily-review.ps1. The
  file is parsed with ConvertFrom-Json and never executed.

  With -LogFile set (the Scheduled Task sets it), each run appends a
  timestamped success or failure line there; a failure also exits non-zero so
  Task Scheduler's "Last Run Result" shows it.
#>
param(
    [string]$BaseUrl = $(if ($env:LAB_TRACKER_BASE_URL) { $env:LAB_TRACKER_BASE_URL } else { "http://127.0.0.1:8000" }),
    [string]$ApiKey = $env:LAB_TRACKER_API_KEY,
    [string]$AdminUser = $env:LAB_TRACKER_ADMIN_USER,
    [string]$AdminPass = $env:LAB_TRACKER_ADMIN_PASS,
    [string]$SecretsFile = $env:LAB_TRACKER_SECRETS_FILE,
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

function Write-RunLog([string]$Message) {
    $line = "$(Get-Date -Format o) lab-tracker: $Message"
    Write-Host $line
    if ($LogFile) {
        Add-Content -LiteralPath $LogFile -Value $line
    }
}

try {
    if ($SecretsFile -and (Test-Path -LiteralPath $SecretsFile)) {
        $secrets = Get-Content -LiteralPath $SecretsFile -Raw | ConvertFrom-Json
        if (-not $ApiKey) { $ApiKey = [string]$secrets.LAB_TRACKER_API_KEY }
        if (-not $AdminUser) { $AdminUser = [string]$secrets.LAB_TRACKER_ADMIN_USER }
        if (-not $AdminPass) { $AdminPass = [string]$secrets.LAB_TRACKER_ADMIN_PASS }
    }

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
    Write-RunLog "daily review run-due triggered ($BaseUrl)"
} catch {
    Write-RunLog "daily review run-due failed ($BaseUrl): $($_.Exception.Message)"
    exit 1
}
