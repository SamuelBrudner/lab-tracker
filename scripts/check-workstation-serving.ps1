[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Username = "",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"

$base = $BaseUrl.TrimEnd("/")

function Invoke-LabTrackerJson {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Headers = $null,
        [object]$Body = $null
    )

    $parameters = @{
        Method = $Method
        Uri = "$base$Path"
        TimeoutSec = 10
    }
    if ($Headers) {
        $parameters.Headers = $Headers
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = ($Body | ConvertTo-Json -Depth 20)
    }

    Invoke-RestMethod @parameters
}

$health = Invoke-LabTrackerJson -Method "GET" -Path "/health"
$readiness = Invoke-LabTrackerJson -Method "GET" -Path "/readiness"

$projectGate = "unknown"
try {
    Invoke-LabTrackerJson -Method "GET" -Path "/projects" | Out-Null
    $projectGate = "open"
} catch {
    if ($_.Exception.Response) {
        $projectGate = "HTTP $([int]$_.Exception.Response.StatusCode)"
    } else {
        $projectGate = $_.Exception.Message
    }
}

$login = $null
if ($Username -and $Password) {
    $login = Invoke-LabTrackerJson -Method "POST" -Path "/auth/login" -Body @{
        username = $Username
        password = $Password
    }
}

[pscustomobject]@{
    base_url = $base
    health = $health.status
    readiness = $readiness.status
    unauthenticated_projects = $projectGate
    login_username = if ($login) { $login.data.user.username } else { $null }
    login_role = if ($login) { $login.data.user.role } else { $null }
} | ConvertTo-Json -Depth 10
