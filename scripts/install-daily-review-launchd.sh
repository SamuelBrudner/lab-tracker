#!/usr/bin/env sh
# Install a macOS launchd LaunchAgent that runs Lab Tracker's daily review on a
# schedule. Preferred over cron on macOS: a LaunchAgent survives reboots, runs
# in your user session, and is managed with launchctl. On Linux/Windows use
# scripts/install-daily-review.sh (cron) or the Windows installers instead.
#
# It polls /batches/run-due every N minutes so each project fires at its own
# configured local time. Polling is cheap and idempotent -- off-time polls find
# nothing due.
#
# Usage:
#   scripts/install-daily-review-launchd.sh [interval_minutes] [base_url]
#
# Examples:
#   scripts/install-daily-review-launchd.sh                 # every 15 min, localhost
#   scripts/install-daily-review-launchd.sh 15 https://lab.example.org
#
# For an auth-enabled deployment, prefer a scoped API token:
#   export LAB_TRACKER_API_KEY=lpat_...   # a scope=batch_run_due token
# LAB_TRACKER_ADMIN_USER / LAB_TRACKER_ADMIN_PASS still work as a fallback. Any
# secret you export is written to a 0600 JSON file (NOT the plist) that the agent
# reads structurally at run time -- it is never sourced or shell-evaluated.
set -eu

case "$(uname -s)" in
    Darwin) ;;
    *)
        echo "lab-tracker: launchd is macOS-only. Use scripts/install-daily-review.sh (cron)." >&2
        exit 1
        ;;
esac

INTERVAL="${1:-15}"
BASE_URL="${2:-http://127.0.0.1:8000}"
LABEL="com.lab-tracker.daily-review"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIGGER="$SCRIPT_DIR/daily-review-run-due.sh"
SCHED="$SCRIPT_DIR/scheduler_install.py"
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
SECRETS_DIR="$HOME/.config/lab-tracker"
SECRETS_FILE="$SECRETS_DIR/daily-review.secrets.json"
LOG="${LAB_TRACKER_DAILY_REVIEW_LOG:-$HOME/.lab-tracker-daily-review.log}"

PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
    echo "lab-tracker: python3 is required to install the daily review safely." >&2
    exit 1
fi
if [ ! -f "$TRIGGER" ]; then
    echo "lab-tracker: trigger script not found at $TRIGGER" >&2
    exit 1
fi
chmod +x "$TRIGGER" 2>/dev/null || true
mkdir -p "$AGENT_DIR" "$SECRETS_DIR"

# Validate schedule up front; reject a non-integer interval or non-http(s) URL.
INTERVAL="$("$PYTHON" "$SCHED" validate-interval "$INTERVAL")"
BASE_URL="$("$PYTHON" "$SCHED" validate-base-url "$BASE_URL")"
INTERVAL_SECONDS=$((INTERVAL * 60))

# Serialize any exported credentials to a private 0600 JSON file. The agent
# reads them back structurally (json.load) -- never `. env`, so a credential
# containing spaces, quotes, $(...) or newlines can neither corrupt the file nor
# execute.
umask 077
LAB_TRACKER_API_KEY="${LAB_TRACKER_API_KEY:-}" \
LAB_TRACKER_ADMIN_USER="${LAB_TRACKER_ADMIN_USER:-}" \
LAB_TRACKER_ADMIN_PASS="${LAB_TRACKER_ADMIN_PASS:-}" \
    "$PYTHON" "$SCHED" write-secrets "$SECRETS_FILE"

# Render the plist in Python (plistlib XML-escapes every value). It runs the
# trigger directly with the secrets-file path + base URL in EnvironmentVariables
# -- there is no `sh -c '. env'` and no secret value in the world-readable plist.
"$PYTHON" "$SCHED" render-plist \
    --label "$LABEL" --trigger "$TRIGGER" \
    --secrets-file "$SECRETS_FILE" --base-url "$BASE_URL" \
    --interval-seconds "$INTERVAL_SECONDS" --log "$LOG" >"$PLIST"

# Validate the generated plist before loading it.
if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$PLIST" >/dev/null
fi

# Reload idempotently: modern launchctl (bootout/bootstrap), legacy fallback.
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
fi

echo "Installed LaunchAgent $LABEL (every $INTERVAL min -> $BASE_URL/batches/run-due)."
echo "  plist:   $PLIST"
echo "  secrets: $SECRETS_FILE (0600)"
echo "  log:     $LOG"
echo ""
echo "Next: enable the daily review per project on the Batches page (/app/batches)."
echo "Status: launchctl print $DOMAIN/$LABEL"
echo "Remove: launchctl bootout $DOMAIN/$LABEL; rm '$PLIST' '$SECRETS_FILE'"
