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
# For an auth-enabled deployment, export LAB_TRACKER_ADMIN_USER /
# LAB_TRACKER_ADMIN_PASS before running. They are written to a 0600 env file
# (NOT into the plist) that the agent sources at run time.
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
AGENT_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENT_DIR/$LABEL.plist"
ENV_DIR="$HOME/.config/lab-tracker"
ENV_FILE="$ENV_DIR/daily-review.env"
LOG="${LAB_TRACKER_DAILY_REVIEW_LOG:-$HOME/.lab-tracker-daily-review.log}"

if [ ! -f "$TRIGGER" ]; then
    echo "lab-tracker: trigger script not found at $TRIGGER" >&2
    exit 1
fi
chmod +x "$TRIGGER" 2>/dev/null || true
mkdir -p "$AGENT_DIR" "$ENV_DIR"

# Secrets live in a 0600 env file the agent sources, never in the world-readable
# plist. umask keeps the file private even if it is created fresh below.
umask 077
{
    printf 'LAB_TRACKER_BASE_URL=%s\n' "$BASE_URL"
    if [ -n "${LAB_TRACKER_ADMIN_USER:-}" ]; then
        printf 'LAB_TRACKER_ADMIN_USER=%s\n' "$LAB_TRACKER_ADMIN_USER"
    fi
    if [ -n "${LAB_TRACKER_ADMIN_PASS:-}" ]; then
        printf 'LAB_TRACKER_ADMIN_PASS=%s\n' "$LAB_TRACKER_ADMIN_PASS"
    fi
} >"$ENV_FILE"
chmod 600 "$ENV_FILE"

INTERVAL_SECONDS=$((INTERVAL * 60))

cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>. "$ENV_FILE"; exec "$TRIGGER"</string>
    </array>
    <key>StartInterval</key>
    <integer>$INTERVAL_SECONDS</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST_EOF

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
echo "  secrets: $ENV_FILE (0600)"
echo "  log:     $LOG"
echo ""
echo "Next: enable the daily review per project on the Batches page (/app/batches)."
echo "Status: launchctl print $DOMAIN/$LABEL"
echo "Remove: launchctl bootout $DOMAIN/$LABEL; rm '$PLIST' '$ENV_FILE'"
