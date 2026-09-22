<#
.SYNOPSIS
  Install a Windows Scheduled Task that runs Lab Tracker's daily review.

.DESCRIPTION
  This is the one-command setup. It registers a task that polls
  /batches/run-due every N minutes, so each project fires at its own configured
  local time. Polling is cheap and idempotent -- off-time polls find nothing due.

  Re-running updates the existing task. The default targets a local Lab Tracker
  with auth disabled, so no credentials are needed.

  When auth is enabled, set LAB_TRACKER_API_KEY (preferred; a scope=batch_run_due
  token) or LAB_TRACKER_ADMIN_USER / LAB_TRACKER_ADMIN_PASS in this PowerShell
  session before running the installer. A Scheduled Task does not inherit this
  session's environment, so the installer persists whichever credential is set
  to a per-user JSON file (-SecretsFile, default
  %LOCALAPPDATA%\LabTracker\daily-review.secrets.json) whose ACL grants access
  to the current user only. The task definition carries only that file's path;
  the trigger reads it structurally (ConvertFrom-Json) at run time. Re-running
  with no credential set keeps an existing secrets file; delete it to clear it.
  Setting only one of LAB_TRACKER_ADMIN_USER / LAB_TRACKER_ADMIN_PASS (e.g. to
  rotate the password) keeps the other from the existing file, and fails if the
  file has none.

  Each run appends a timestamped result line to -LogFile (default
  %LOCALAPPDATA%\LabTracker\daily-review.log).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts/install-daily-review.ps1

.EXAMPLE
  $env:LAB_TRACKER_API_KEY = "lpat_..."
  powershell -ExecutionPolicy Bypass -File scripts/install-daily-review.ps1 -IntervalMinutes 30 -BaseUrl https://lab.example.org
#>
param(
    [int]$IntervalMinutes = 15,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$TaskName = "LabTrackerDailyReview",
    [string]$SecretsFile = $(Join-Path $env:LOCALAPPDATA "LabTracker\daily-review.secrets.json"),
    [string]$LogFile = $(Join-Path $env:LOCALAPPDATA "LabTracker\daily-review.log")
)

$ErrorActionPreference = "Stop"

# Same charset as scripts/scheduler_install.py validate_base_url: the URL is
# interpolated into the task's quoted argument string, so a quote, backtick,
# `$`, `;`, whitespace or newline must never reach it.
$SafeBaseUrlPattern = '^https?://[A-Za-z0-9._:/\-]+\z'
if ($BaseUrl -cnotmatch $SafeBaseUrlPattern) {
    throw "BaseUrl must be an http(s) URL containing only letters, digits, and ._:/- ; got '$BaseUrl'."
}
$BaseUrl = $BaseUrl.TrimEnd("/")
if ($IntervalMinutes -lt 1 -or $IntervalMinutes -gt 1440) {
    throw "IntervalMinutes must be between 1 and 1440; got $IntervalMinutes."
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$trigger = Join-Path $scriptDir "daily-review-run-due.ps1"
if (-not (Test-Path $trigger)) {
    throw "Trigger script not found: $trigger"
}

function Assert-NotReparsePoint([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to use '$Path': it is a symbolic link or junction."
    }
}

function Set-PrivateAcl([string]$Path) {
    # Disable inheritance, drop inherited rules, and grant only the current user.
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($user, "FullControl", "Allow")
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

$secretsDir = Split-Path -Parent $SecretsFile
New-Item -ItemType Directory -Force -Path $secretsDir | Out-Null
Assert-NotReparsePoint $secretsDir
Assert-NotReparsePoint $SecretsFile
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null

$credentials = [ordered]@{}
foreach ($pair in @(
        @("LAB_TRACKER_API_KEY", $env:LAB_TRACKER_API_KEY),
        @("LAB_TRACKER_ADMIN_USER", $env:LAB_TRACKER_ADMIN_USER),
        @("LAB_TRACKER_ADMIN_PASS", $env:LAB_TRACKER_ADMIN_PASS))) {
    if ($pair[1]) { $credentials[$pair[0]] = [string]$pair[1] }
}

# Only one half of the admin login set (e.g. a password rotation): carry the
# other half over from the stored file, or refuse before anything is written,
# so the file never ends up holding a login that cannot work.
if ($credentials.Contains("LAB_TRACKER_ADMIN_USER") -xor $credentials.Contains("LAB_TRACKER_ADMIN_PASS")) {
    $missing = if ($credentials.Contains("LAB_TRACKER_ADMIN_USER")) { "LAB_TRACKER_ADMIN_PASS" } else { "LAB_TRACKER_ADMIN_USER" }
    $stored = $null
    if (Test-Path -LiteralPath $SecretsFile) {
        # Fails loudly on malformed JSON rather than silently discarding it.
        $stored = (Get-Content -LiteralPath $SecretsFile -Raw | ConvertFrom-Json).$missing
    }
    if (-not $stored) {
        throw ("$missing is not set in this session and $SecretsFile has no stored $missing to keep; " +
            "export both LAB_TRACKER_ADMIN_USER and LAB_TRACKER_ADMIN_PASS and re-run the installer.")
    }
    $credentials[$missing] = [string]$stored
    Write-Host "$missing is not set; keeping the stored $missing from $SecretsFile."
}

$hasCredential = $false
if ($credentials.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $SecretsFile)) {
        New-Item -ItemType File -Path $SecretsFile | Out-Null
    }
    Set-PrivateAcl -Path $SecretsFile
    $json = $credentials | ConvertTo-Json -Compress
    [System.IO.File]::WriteAllText($SecretsFile, $json, (New-Object System.Text.UTF8Encoding $false))
    $hasCredential = $true
    Write-Host "Persisted credentials to $SecretsFile (current user only)."
} elseif (Test-Path -LiteralPath $SecretsFile) {
    # Fails loudly on malformed JSON rather than silently discarding it.
    $existing = Get-Content -LiteralPath $SecretsFile -Raw | ConvertFrom-Json
    Set-PrivateAcl -Path $SecretsFile
    $hasCredential = [bool]($existing.LAB_TRACKER_API_KEY -or $existing.LAB_TRACKER_ADMIN_USER)
    Write-Host "No LAB_TRACKER_API_KEY / LAB_TRACKER_ADMIN_USER set; keeping the existing credentials in $SecretsFile. Delete it to clear them."
}

$baseHost = ([System.Uri]$BaseUrl).Host
if (-not $hasCredential -and @("127.0.0.1", "localhost", "[::1]", "::1") -notcontains $baseHost) {
    Write-Warning ("No credential is persisted for $BaseUrl. If that server has authentication " +
        "enabled every scheduled run will fail with 401. Set LAB_TRACKER_API_KEY in this " +
        "session and re-run the installer.")
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$trigger`" -BaseUrl `"$BaseUrl`" -SecretsFile `"$SecretsFile`" -LogFile `"$LogFile`""

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
Write-Host "  log: $LogFile"
Write-Host ""
Write-Host "Next: enable the daily review per project on the Batches page (/app/batches)."
Write-Host "Remove with: Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false; Remove-Item '$SecretsFile' -ErrorAction SilentlyContinue"
