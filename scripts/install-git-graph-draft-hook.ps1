param(
    [Parameter(Mandatory = $true)]
    [string]$TargetRepo,

    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$BaseUrl = "http://127.0.0.1:8000",

    [string]$PythonPath = "",

    [switch]$Force
)

$ErrorActionPreference = "Stop"

function Resolve-GitPath {
    param(
        [string]$Repo,
        [string]$GitPath
    )

    $raw = git -C $Repo rev-parse --git-path $GitPath
    if ([System.IO.Path]::IsPathRooted($raw)) {
        return $raw
    }
    return Join-Path $Repo $raw
}

function Convert-ToHookPath {
    param([string]$Path)
    return $Path.Replace("\", "/")
}

$repoRoot = git -C $TargetRepo rev-parse --show-toplevel
$repoRoot = [System.IO.Path]::GetFullPath($repoRoot)
$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$labTrackerRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptPath ".."))
$hookPath = Resolve-GitPath -Repo $repoRoot -GitPath "hooks/post-commit"
$hookDir = Split-Path -Parent $hookPath

if (-not (Test-Path $hookDir)) {
    New-Item -ItemType Directory -Path $hookDir | Out-Null
}

if (-not $PythonPath) {
    $windowsVenvPython = Join-Path $labTrackerRoot ".venv\Scripts\python.exe"
    $posixVenvPython = Join-Path $labTrackerRoot ".venv/bin/python"
    if (Test-Path $windowsVenvPython) {
        $PythonPath = $windowsVenvPython
    } elseif (Test-Path $posixVenvPython) {
        $PythonPath = $posixVenvPython
    } else {
        $PythonPath = "python"
    }
}

$beginMarker = "# --- BEGIN LAB TRACKER GRAPH DRAFT HOOK ---"
$endMarker = "# --- END LAB TRACKER GRAPH DRAFT HOOK ---"
$labTrackerRootForHook = Convert-ToHookPath $labTrackerRoot
$pythonForHook = Convert-ToHookPath $PythonPath

$managedBlock = @"
$beginMarker
LAB_TRACKER_GIT_DRAFT_ENABLED="`${LAB_TRACKER_GIT_DRAFT_ENABLED:-1}"
if [ "`$LAB_TRACKER_GIT_DRAFT_ENABLED" != "0" ]; then
  LAB_TRACKER_ROOT="`${LAB_TRACKER_ROOT:-$labTrackerRootForHook}"
  LAB_TRACKER_BASE_URL="`${LAB_TRACKER_BASE_URL:-$BaseUrl}"
  LAB_TRACKER_PROJECT_ID="`${LAB_TRACKER_PROJECT_ID:-$ProjectId}"
  LAB_TRACKER_PYTHON="`${LAB_TRACKER_PYTHON:-$pythonForHook}"
  LAB_TRACKER_SCRIPT="`$LAB_TRACKER_ROOT/scripts/create-analysis-graph-draft.py"

  if [ -n "`$LAB_TRACKER_PROJECT_ID" ] && [ -f "`$LAB_TRACKER_SCRIPT" ]; then
    repo_root="`$(git rev-parse --show-toplevel 2>/dev/null)"
    commit_sha="`$(git rev-parse HEAD 2>/dev/null)"
    if [ -n "`$repo_root" ] && [ -n "`$commit_sha" ]; then
      "`$LAB_TRACKER_PYTHON" "`$LAB_TRACKER_SCRIPT" \
        --project-id "`$LAB_TRACKER_PROJECT_ID" \
        --git-repo "`$repo_root" \
        --git-commit "`$commit_sha" \
        --base-url "`$LAB_TRACKER_BASE_URL" \
        >/dev/null 2>&1 || echo "lab-tracker: graph draft hook could not create a proposal; commit kept." >&2
    fi
  else
    echo "lab-tracker: graph draft hook is not configured; set LAB_TRACKER_PROJECT_ID and LAB_TRACKER_ROOT." >&2
  fi
fi
$endMarker
"@

$shebang = "#!/usr/bin/env sh"
if (Test-Path $hookPath) {
    $existing = Get-Content -Raw -Path $hookPath
    if ($existing.Contains($beginMarker) -and $existing.Contains($endMarker)) {
        $pattern = "(?s)$([regex]::Escape($beginMarker)).*?$([regex]::Escape($endMarker))"
        $updated = [regex]::Replace($existing, $pattern, $managedBlock)
    } elseif ($Force) {
        $updated = ($existing.TrimEnd() + "`n`n" + $managedBlock + "`n")
    } elseif ($existing.Trim()) {
        throw "Existing post-commit hook is not Lab Tracker-managed. Re-run with -Force to append the managed block."
    } else {
        $updated = "$shebang`n$managedBlock`n"
    }
} else {
    $updated = "$shebang`n$managedBlock`n"
}

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($hookPath, $updated.Replace("`r`n", "`n"), $utf8NoBom)

Write-Host "Installed Lab Tracker graph draft post-commit hook:"
Write-Host "  Repo: $repoRoot"
Write-Host "  Hook: $hookPath"
Write-Host "  Project: $ProjectId"
Write-Host "  API: $BaseUrl"
