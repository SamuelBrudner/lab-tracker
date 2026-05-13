[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$UsePostgres,
    [switch]$SkipMigrations,
    [switch]$PrintOnly,
    [string]$ExternalBaseUrl = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $repoRoot

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

function Test-LabTrackerTailscaleIPv4 {
    param([string]$Address)

    $parts = $Address -split "\."
    if ($parts.Count -ne 4) {
        return $false
    }

    $first = 0
    $second = 0
    if (-not [int]::TryParse($parts[0], [ref]$first)) {
        return $false
    }
    if (-not [int]::TryParse($parts[1], [ref]$second)) {
        return $false
    }

    return $first -eq 100 -and $second -ge 64 -and $second -le 127
}

function Add-LabTrackerUniqueHost {
    param(
        [System.Collections.Generic.List[string]]$Hosts,
        [string]$HostName
    )

    $normalized = $HostName.Trim().TrimEnd(".")
    if ($normalized -and -not $Hosts.Contains($normalized)) {
        $Hosts.Add($normalized)
    }
}

function Get-LabTrackerLanAddress {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.InterfaceAlias -notlike "vEthernet*" -and
            -not (Test-LabTrackerTailscaleIPv4 $_.IPAddress) -and
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
            $_.IPAddress -notlike "169.254.*" -and
            -not (Test-LabTrackerTailscaleIPv4 $_.IPAddress)
        } |
        Sort-Object InterfaceAlias, IPAddress |
        Select-Object -ExpandProperty IPAddress -Unique
}

function Get-LabTrackerTailscaleHost {
    $hosts = [System.Collections.Generic.List[string]]::new()
    $tailscale = Get-Command "tailscale" -ErrorAction SilentlyContinue

    if ($tailscale) {
        $ipOutput = & $tailscale.Source ip -4 2>$null
        if ($LASTEXITCODE -eq 0) {
            foreach ($line in $ipOutput) {
                Add-LabTrackerUniqueHost -Hosts $hosts -HostName $line
            }
        }

        $statusOutput = & $tailscale.Source status --json 2>$null | Out-String
        if ($LASTEXITCODE -eq 0 -and $statusOutput.Trim()) {
            try {
                $status = $statusOutput | ConvertFrom-Json
                if ($status.Self.DNSName) {
                    Add-LabTrackerUniqueHost -Hosts $hosts -HostName $status.Self.DNSName
                }
            } catch {
                # A stale or older tailscale client should not prevent serving.
            }
        }
    }

    $netAddresses = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { Test-LabTrackerTailscaleIPv4 $_.IPAddress } |
        Sort-Object InterfaceAlias, IPAddress |
        Select-Object -ExpandProperty IPAddress -Unique
    foreach ($address in $netAddresses) {
        Add-LabTrackerUniqueHost -Hosts $hosts -HostName $address
    }

    return $hosts.ToArray()
}

function Get-LabTrackerAuthSummary {
    $environment = if ($env:LAB_TRACKER_ENVIRONMENT) { $env:LAB_TRACKER_ENVIRONMENT } else { "local" }
    if ($env:LAB_TRACKER_AUTH_ENABLED) {
        return "environment=$environment, LAB_TRACKER_AUTH_ENABLED=$($env:LAB_TRACKER_AUTH_ENABLED)"
    }
    if ($environment.Trim().ToLowerInvariant() -eq "local") {
        return "environment=local, auth disabled by default"
    }
    return "environment=$environment, auth enabled by default"
}

$lanAddresses = @(Get-LabTrackerLanAddress)
$tailscaleHosts = @(Get-LabTrackerTailscaleHost)
$trimmedExternalBaseUrl = $ExternalBaseUrl.Trim().TrimEnd("/")

Write-Host "Lab Tracker shared graph serving"
Write-Host ""
Write-Host "Repo: $repoRoot"
Write-Host "Python: $Python"
Write-Host "Bind: 0.0.0.0:$Port"
Write-Host "Auth: $(Get-LabTrackerAuthSummary)"
if ($env:LAB_TRACKER_DATABASE_URL) {
    Write-Host "Database: $($env:LAB_TRACKER_DATABASE_URL)"
} else {
    Write-Host "Database: repo/default settings"
    Write-Host "Tip: use -UsePostgres for the recommended multi-client runtime."
}
Write-Host ""

if ($lanAddresses.Count -gt 0) {
    Write-Host "Same LAN or conventional VPN URLs:"
    foreach ($address in $lanAddresses) {
        Write-Host "  http://${address}:$Port/health"
        Write-Host "  http://${address}:$Port/app"
    }
} else {
    Write-Host "No non-loopback IPv4 address was detected. Check your network connection."
}

if ($tailscaleHosts.Count -gt 0) {
    Write-Host ""
    Write-Host "Tailscale tailnet URLs:"
    foreach ($hostName in $tailscaleHosts) {
        Write-Host "  http://${hostName}:$Port/health"
        Write-Host "  http://${hostName}:$Port/app"
    }
}

if ($trimmedExternalBaseUrl) {
    Write-Host ""
    Write-Host "External tunnel URLs:"
    Write-Host "  $trimmedExternalBaseUrl/health"
    Write-Host "  $trimmedExternalBaseUrl/app"
}

Write-Host ""
Write-Host "If remote computers time out, run this on the host in PowerShell as Administrator:"
Write-Host "  New-NetFirewallRule -DisplayName `"Lab Tracker TCP $Port`" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Domain,Private"
Write-Host ""
Write-Host "For remote access, keep authentication enabled and set LAB_TRACKER_AUTH_SECRET_KEY."
Write-Host "For MCP clients, set LAB_TRACKER_MCP_BASE_URL to the base URL above without /app."
Write-Host ""

if ($PrintOnly) {
    exit 0
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
