#!/usr/bin/env sh
# Install a cron entry that runs Lab Tracker's daily review on a schedule.
#
# This is the one-command setup: it polls /batches/run-due every N minutes so
# that each project fires at its own configured local time. Polling is cheap and
# idempotent -- off-time polls find nothing due.
#
# Usage:
#   scripts/install-daily-review.sh [interval_minutes] [base_url]
#
# Examples:
#   scripts/install-daily-review.sh                 # every 15 min, localhost
#   scripts/install-daily-review.sh 30 https://lab.example.org
#
# For an auth-enabled deployment, also export LAB_TRACKER_ADMIN_USER /
# LAB_TRACKER_ADMIN_PASS in the environment cron runs under.
set -eu

INTERVAL="${1:-15}"
BASE_URL="${2:-http://127.0.0.1:8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIGGER="$SCRIPT_DIR/daily-review-run-due.sh"
LOG="${LAB_TRACKER_DAILY_REVIEW_LOG:-$HOME/.lab-tracker-daily-review.log}"
TAG="# lab-tracker-daily-review"

if ! command -v crontab >/dev/null 2>&1; then
    echo "lab-tracker: crontab not found. Use a different scheduler (see docs/scheduled-daily-review.md)." >&2
    exit 1
fi

chmod +x "$TRIGGER" 2>/dev/null || true

LINE="*/$INTERVAL * * * * LAB_TRACKER_BASE_URL=\"$BASE_URL\" \"$TRIGGER\" >> \"$LOG\" 2>&1 $TAG"

# Idempotent: drop any prior lab-tracker line, then add the current one.
( crontab -l 2>/dev/null | grep -v "$TAG" || true ; echo "$LINE" ) | crontab -

echo "Installed cron entry (every $INTERVAL min -> $BASE_URL/batches/run-due):"
echo "  $LINE"
echo ""
echo "Next: enable the daily review per project on the Batches page (/app/batches)."
echo "Remove with: crontab -l | grep -v '$TAG' | crontab -"
