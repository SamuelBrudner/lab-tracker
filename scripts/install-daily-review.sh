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
# For an auth-enabled deployment, prefer a scoped API token:
#   export LAB_TRACKER_API_KEY=lpat_...   # a scope=batch_run_due token
# (issue one via POST /auth/tokens with "scope":"batch_run_due"). It can trigger
# ONLY the due-batch run, so a leaked scheduler token cannot read or write
# anything else. LAB_TRACKER_ADMIN_USER / LAB_TRACKER_ADMIN_PASS still work as a
# fallback.
set -eu

INTERVAL="${1:-15}"
BASE_URL="${2:-http://127.0.0.1:8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRIGGER="$SCRIPT_DIR/daily-review-run-due.sh"
SCHED="$SCRIPT_DIR/scheduler_install.py"
LOG="${LAB_TRACKER_DAILY_REVIEW_LOG:-$HOME/.lab-tracker-daily-review.log}"
TAG="# lab-tracker-daily-review"

PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
    echo "lab-tracker: python3 is required to install the daily review safely." >&2
    exit 1
fi
if ! command -v crontab >/dev/null 2>&1; then
    echo "lab-tracker: crontab not found. Use a different scheduler (see docs/scheduled-daily-review.md)." >&2
    exit 1
fi

chmod +x "$TRIGGER" 2>/dev/null || true

# Build + validate the cron line in Python (rejects a non-integer interval or a
# non-http(s) base URL) so nothing unchecked is interpolated into the crontab.
LINE="$("$PYTHON" "$SCHED" cron-line \
    --interval "$INTERVAL" --base-url "$BASE_URL" \
    --trigger "$TRIGGER" --log "$LOG" --tag "$TAG")"

# Capture crontab -l stdout, stderr, and exit code separately. The Python merge
# treats ONLY the recognized "no crontab" signal as empty; any other read error
# leaves the crontab untouched and exits non-zero (fail closed), so a transient
# or permission failure can never wipe unrelated jobs.
CRON_STDOUT="$(mktemp)"
CRON_STDERR="$(mktemp)"
trap 'rm -f "$CRON_STDOUT" "$CRON_STDERR"' EXIT

set +e
crontab -l >"$CRON_STDOUT" 2>"$CRON_STDERR"
CRON_EC=$?
MERGED="$("$PYTHON" "$SCHED" merge-crontab \
    --exit-code "$CRON_EC" --stderr "$(cat "$CRON_STDERR")" \
    --tag "$TAG" --line "$LINE" <"$CRON_STDOUT")"
MERGE_EC=$?
set -e

if [ "$MERGE_EC" -ne 0 ]; then
    echo "lab-tracker: leaving your crontab unchanged." >&2
    exit 1
fi

printf '%s' "$MERGED" | crontab -

echo "Installed cron entry (every $INTERVAL min -> $BASE_URL/batches/run-due):"
echo "  $LINE"
echo ""
echo "Next: enable the daily review per project on the Batches page (/app/batches)."
echo "Remove with: crontab -l | grep -v '$TAG' | crontab -"
