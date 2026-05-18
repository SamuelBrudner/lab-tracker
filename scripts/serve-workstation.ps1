[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$HostAddress = "127.0.0.1",
    [string]$EnvFile = ".env.workstation",
    [switch]$UsePostgres,
    [switch]$SkipMigrations,
    [switch]$PrintOnly,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

function Import-LabTrackerEnvFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            continue
        }

        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path "Env:$name" -Value $value
    }
}

if (-not $Python) {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $Python = $venvPython
    } else {
        $Python = "python"
    }
}

Import-LabTrackerEnvFile -Path $EnvFile

if ($UsePostgres -and -not $env:LAB_TRACKER_DATABASE_URL) {
    $env:LAB_TRACKER_DATABASE_URL = "postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker"
}

if (-not $env:LAB_TRACKER_ENVIRONMENT) {
    $env:LAB_TRACKER_ENVIRONMENT = "production"
}
if (-not $env:LAB_TRACKER_AUTH_ENABLED) {
    $env:LAB_TRACKER_AUTH_ENABLED = "true"
}

$secret = ($env:LAB_TRACKER_AUTH_SECRET_KEY | ForEach-Object { "$_".Trim() })
if (
    -not $secret -or
    $secret -eq "dev-only-change-me" -or
    $secret -eq "replace-with-a-strong-secret" -or
    $secret.StartsWith("<")
) {
    throw "Set LAB_TRACKER_AUTH_SECRET_KEY in $EnvFile before serving the workstation."
}

if ($env:LAB_TRACKER_AUTH_ENABLED.Trim().ToLowerInvariant() -eq "false") {
    throw "Workstation serving requires LAB_TRACKER_AUTH_ENABLED=true."
}

$baseUrl = "http://${HostAddress}:$Port"
$tunnelTarget = "http://127.0.0.1:$Port"

Write-Host "Lab Tracker workstation serving"
Write-Host ""
Write-Host "Repo: $repoRoot"
Write-Host "Python: $Python"
Write-Host "Bind: ${HostAddress}:$Port"
Write-Host "Local URL: $baseUrl/app"
Write-Host "Cloudflare tunnel target: $tunnelTarget"
Write-Host "Environment: $env:LAB_TRACKER_ENVIRONMENT"
Write-Host "Auth enabled: $env:LAB_TRACKER_AUTH_ENABLED"
if ($env:LAB_TRACKER_DATABASE_URL) {
    Write-Host "Database: $env:LAB_TRACKER_DATABASE_URL"
} else {
    Write-Host "Database: repo/default settings"
}
Write-Host ""

if ($HostAddress -ne "127.0.0.1" -and $HostAddress -ne "localhost") {
    Write-Host "Warning: workstation serving is intended for localhost plus an HTTPS tunnel."
    Write-Host "Use scripts/serve-lan.ps1 when direct LAN access is the goal."
    Write-Host ""
}

if ($PrintOnly) {
    exit 0
}

$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $listenerProcessIds = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Port $Port is already in use by process id(s): $listenerProcessIds."
}

if (-not $SkipMigrations) {
    & $Python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

& $Python -m uvicorn lab_tracker.asgi:app --host $HostAddress --port $Port
