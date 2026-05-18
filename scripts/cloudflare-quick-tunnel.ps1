[CmdletBinding()]
param(
    [string]$LocalUrl = "http://127.0.0.1:8000",
    [string]$Cloudflared = "",
    [switch]$SkipHealthCheck
)

$ErrorActionPreference = "Stop"

if (-not $Cloudflared) {
    $command = Get-Command "cloudflared" -ErrorAction SilentlyContinue
    if ($command) {
        $Cloudflared = $command.Source
    }
}

if (-not $Cloudflared) {
    throw @"
cloudflared was not found.

Install it, then rerun this command. On Windows, one option is:
  winget install --id Cloudflare.cloudflared

Cloudflare docs:
  https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
"@
}

$trimmedLocalUrl = $LocalUrl.TrimEnd("/")

if (-not $SkipHealthCheck) {
    $healthUrl = "$trimmedLocalUrl/health"
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 5
        if ($health.status -ne "ok") {
            throw "unexpected health payload: $($health | ConvertTo-Json -Compress)"
        }
    } catch {
        throw "Lab Tracker did not answer $healthUrl. Start scripts/serve-workstation.ps1 first. $($_.Exception.Message)"
    }
}

Write-Host "Starting Cloudflare Quick Tunnel"
Write-Host ""
Write-Host "Local service: $trimmedLocalUrl"
Write-Host "Watch the cloudflared output for a https://*.trycloudflare.com URL."
Write-Host "Stop the tunnel with Ctrl+C when the test is done."
Write-Host ""

& $Cloudflared tunnel --url $trimmedLocalUrl
