<#
.SYNOPSIS
    Trigger due daily reviews, then email a contentless "your review is ready"
    cue to a reviewer for each freshly-ready edition.

.DESCRIPTION
    Reference implementation of the daily-review email cue (see
    docs/scheduled-daily-review.md and docs/daily-review-egress-defaults.md).

    Lab Tracker holds no mail credentials and owns no transport: this script runs
    on YOUR machine and sends through YOUR mailbox. It is deliberately
    contentless -- the email carries a fixed phrase, an item count, and a signed
    link, never any science.

    Flow per run:
      1. POST /batches/run-due   (trigger drafting; admin token)
      2. GET  /batches/editions-ready?user_id=<reviewer>&since=<watermark>
      3. For each edition newer than the stored watermark, send one email
         carrying the fixed cue phrase + count + signed deep link.
      4. Advance the watermark file to the newest edition's created_at.

    Schedule it (Windows Task Scheduler / the install-daily-review.ps1 pattern)
    to poll every ~15 minutes; the per-(project,user) run_at_local_time decides
    when a review actually generates.

.NOTES
    Requires an ADMIN token with the "Scheduler trigger (admin)" level, minted at
    /app/agents. Never commit real credentials; pass them via environment.
#>

[CmdletBinding()]
param(
    [string]$BaseUrl       = $env:LAB_TRACKER_BASE_URL,
    [string]$ApiKey        = $env:LAB_TRACKER_API_KEY,      # lpat_... admin, read-only
    [string]$ReviewerUserId = $env:LAB_TRACKER_REVIEW_USER_ID,
    [string]$To            = $env:LAB_TRACKER_REVIEW_EMAIL,
    [string]$From          = $env:LAB_TRACKER_MAIL_FROM,
    [string]$SmtpServer    = $env:LAB_TRACKER_SMTP_SERVER,
    [int]   $SmtpPort      = $(if ($env:LAB_TRACKER_SMTP_PORT) { [int]$env:LAB_TRACKER_SMTP_PORT } else { 587 }),
    [string]$SmtpUser      = $env:LAB_TRACKER_SMTP_USER,
    [string]$SmtpPassword  = $env:LAB_TRACKER_SMTP_PASSWORD,
    [string]$WatermarkFile = $(if ($env:LAB_TRACKER_REVIEW_WATERMARK) { $env:LAB_TRACKER_REVIEW_WATERMARK } else { Join-Path $HOME '.lab-tracker/review-cue-watermark.txt' }),
    [switch]$SkipRunDue,   # if run-due is triggered by a separate job
    [switch]$WhatIf        # compose + log but do not send
)

$ErrorActionPreference = 'Stop'

foreach ($pair in @{ BaseUrl = $BaseUrl; ApiKey = $ApiKey; ReviewerUserId = $ReviewerUserId; To = $To }.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace($pair.Value)) {
        throw "Missing required setting '$($pair.Key)'. Set it via -$($pair.Key) or the matching LAB_TRACKER_* env var."
    }
}
$BaseUrl = $BaseUrl.TrimEnd('/')
$headers = @{ Authorization = "Bearer $ApiKey" }

# 1. Trigger due drafts (idempotent; off-time polls find nothing and return fast).
if (-not $SkipRunDue) {
    try {
        Invoke-RestMethod -Method Post -Uri "$BaseUrl/batches/run-due" -Headers $headers -TimeoutSec 60 | Out-Null
    } catch {
        Write-Warning "run-due failed: $($_.Exception.Message)"
    }
}

# 2. Read the watermark and query ready editions for this reviewer.
$since = $null
if (Test-Path $WatermarkFile) { $since = (Get-Content -Raw $WatermarkFile).Trim() }

$query = "user_id=$([uri]::EscapeDataString($ReviewerUserId))"
if ($since) {
    # An ISO-8601 timestamp ends in '+00:00'; the '+' MUST be percent-encoded or
    # the server reads it as a space and rejects the request.
    $query += "&since=$([uri]::EscapeDataString($since))"
}
$response = Invoke-RestMethod -Method Get -Uri "$BaseUrl/batches/editions-ready?$query" -Headers $headers -TimeoutSec 60
$editions = @($response.data)

if ($editions.Count -eq 0) {
    Write-Host "No new ready editions for $ReviewerUserId (watermark: $since)."
    return
}

# 3. Send one contentless cue per edition.
$newest = $since
foreach ($edition in $editions) {
    $count = $edition.decidable_count
    $countPhrase = if ($null -ne $count) { "$count thing$(if ($count -ne 1) { 's' } else { '' })" } else { "some notes" }
    $subject = "Your Lab Tracker daily review is ready"
    $body = @"
Your Lab Tracker daily review is ready — $countPhrase to look over.

Open it: $($edition.deep_link)

(You review and decide inside the app; this note carries no details.)
"@

    if ($WhatIf) {
        Write-Host "[WhatIf] would email ${To}: $subject / $($edition.deep_link)"
    } else {
        $sendParams = @{
            To         = $To
            From       = $From
            Subject    = $subject
            Body       = $body
            SmtpServer = $SmtpServer
            Port       = $SmtpPort
            UseSsl     = $true
        }
        if ($SmtpUser) {
            $secure = ConvertTo-SecureString $SmtpPassword -AsPlainText -Force
            $sendParams.Credential = New-Object System.Management.Automation.PSCredential($SmtpUser, $secure)
        }
        # Send-MailMessage is obsolete but adequate for a single self-hosted cue;
        # swap in your lab's preferred mailer if you have one.
        Send-MailMessage @sendParams
        Write-Host "Sent cue to $To for edition $($edition.change_set_id)."
    }

    if (-not $newest -or ([datetime]$edition.created_at -gt [datetime]$newest)) {
        $newest = $edition.created_at
    }
}

# 4. Advance the watermark so the same edition is never cued twice.
if (-not $WhatIf -and $newest) {
    $dir = Split-Path -Parent $WatermarkFile
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Set-Content -Path $WatermarkFile -Value $newest -Encoding utf8
}
