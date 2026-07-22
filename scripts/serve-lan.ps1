[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$UsePostgres,
    [switch]$SkipMigrations,
    [switch]$PrintOnly,
    [switch]$AllowInsecureAuthDisabled,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot
$srcPath = Join-Path $repoRoot "src"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$srcPath;$($env:PYTHONPATH)"
} else {
    $env:PYTHONPATH = $srcPath
}

if (-not $Python) {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $Python = $venvPython
    } else {
        $Python = "python"
    }
}

if ($UsePostgres) {
    $env:LAB_TRACKER_DATABASE_URL = "postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker"
}

function Get-LabTrackerLanAddress {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.InterfaceAlias -notlike "vEthernet*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Sort-Object InterfaceAlias, IPAddress |
        Select-Object -ExpandProperty IPAddress -Unique

    if ($addresses) {
        return $addresses
    }

    return Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*"
        } |
        Sort-Object InterfaceAlias, IPAddress |
        Select-Object -ExpandProperty IPAddress -Unique
}

$lanAddresses = @(Get-LabTrackerLanAddress)

Write-Host "Lab Tracker LAN serving"
Write-Host ""
Write-Host "Repo: $repoRoot"
Write-Host "Python: $Python"
Write-Host "Bind: 0.0.0.0:$Port"
if ($env:LAB_TRACKER_DATABASE_URL) {
    # Redact any embedded password before printing so a credential-bearing
    # DATABASE_URL never leaks to the console/logs.
    $schedScript = Join-Path $PSScriptRoot "scheduler_install.py"
    $redactedDbUrl = (& $Python $schedScript redact-url $env:LAB_TRACKER_DATABASE_URL | Out-String).Trim()
    Write-Host "Database: $redactedDbUrl"
} else {
    Write-Host "Database: repo/default settings"
    Write-Host "Tip: use -UsePostgres for the recommended multi-client runtime."
}
Write-Host ""

if ($lanAddresses.Count -gt 0) {
    Write-Host "Try these URLs from another computer on the same LAN or VPN:"
    foreach ($address in $lanAddresses) {
        Write-Host "  http://${address}:$Port/health"
        Write-Host "  http://${address}:$Port/app"
    }
} else {
    Write-Host "No non-loopback IPv4 address was detected. Check your network connection."
}

Write-Host ""
Write-Host "If remote computers time out, run this on the host in PowerShell as Administrator:"
Write-Host "  New-NetFirewallRule -DisplayName `"Lab Tracker TCP $Port`" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Domain,Private"
Write-Host ""
Write-Host "For LAN or VPN access, keep authentication enabled and set LAB_TRACKER_AUTH_SECRET_KEY."
Write-Host ""

if ($PrintOnly) {
    exit 0
}

$authEnabledOutput = & $Python -c "from lab_tracker.config import get_settings; print('true' if get_settings().is_auth_enabled() else 'false')" 2>&1
if ($LASTEXITCODE -ne 0) {
    $authError = ($authEnabledOutput | Out-String).Trim()
    throw "Unable to evaluate Lab Tracker auth settings before LAN serving. $authError"
}
$authEnabled = (($authEnabledOutput | Select-Object -Last 1) -as [string]).Trim().ToLowerInvariant()
$envOverride = ($env:LAB_TRACKER_ALLOW_INSECURE_AUTH_DISABLED -as [string]).Trim().ToLowerInvariant()
$overrideRequested = $AllowInsecureAuthDisabled -or $envOverride -in @("1", "true", "yes")
if ($authEnabled -ne "true" -and -not $overrideRequested) {
    throw "Refusing to bind Lab Tracker to 0.0.0.0 while authentication is disabled. Set LAB_TRACKER_AUTH_ENABLED=true and LAB_TRACKER_AUTH_SECRET_KEY, or rerun with -AllowInsecureAuthDisabled only for a trusted temporary LAN."
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $listenerProcessIds = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Port $Port is already in use by process id(s): $listenerProcessIds. Stop the existing server or choose another port."
}

if (-not $SkipMigrations) {
    & $Python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

& $Python -m uvicorn lab_tracker.asgi:app --host 0.0.0.0 --port $Port
