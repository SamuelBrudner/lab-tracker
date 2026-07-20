#!/usr/bin/env sh
# Trigger one pass of Lab Tracker's daily review.
#
# POSTs /batches/run-due, which drafts over staged notes for every project whose
# batch settings are enabled and due. This only ever proposes a draft for human
# review -- it never commits anything to your graph.
#
# Configuration (all optional):
#   LAB_TRACKER_BASE_URL      API base URL        (default http://127.0.0.1:8000)
#   LAB_TRACKER_API_KEY       bearer token        (preferred; a scope=batch_run_due lpat_ token)
#   LAB_TRACKER_SECRETS_FILE  0600 JSON secrets   (written by the launchd installer)
#   LAB_TRACKER_ADMIN_USER    admin username      (fallback when auth is enabled)
#   LAB_TRACKER_ADMIN_PASS    admin password      (fallback when auth is enabled)
#
# Exit non-zero if the API is unreachable or login fails.
set -eu

BASE_URL="${LAB_TRACKER_BASE_URL:-http://127.0.0.1:8000}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHED="$SCRIPT_DIR/scheduler_install.py"
PYTHON="$(command -v python3 || command -v python || true)"

# Load secrets from the 0600 JSON file structurally (never sourced/evaluated).
# Command substitution captures each value verbatim, so shell metacharacters in
# a credential cannot execute.
if [ -n "${LAB_TRACKER_SECRETS_FILE:-}" ] && [ -n "$PYTHON" ]; then
  : "${LAB_TRACKER_API_KEY:=$("$PYTHON" "$SCHED" read-secret "$LAB_TRACKER_SECRETS_FILE" LAB_TRACKER_API_KEY)}"
  : "${LAB_TRACKER_ADMIN_USER:=$("$PYTHON" "$SCHED" read-secret "$LAB_TRACKER_SECRETS_FILE" LAB_TRACKER_ADMIN_USER)}"
  : "${LAB_TRACKER_ADMIN_PASS:=$("$PYTHON" "$SCHED" read-secret "$LAB_TRACKER_SECRETS_FILE" LAB_TRACKER_ADMIN_PASS)}"
fi

auth_header=""
if [ -n "${LAB_TRACKER_API_KEY:-}" ]; then
  auth_header="Authorization: Bearer $LAB_TRACKER_API_KEY"
elif [ -n "${LAB_TRACKER_ADMIN_USER:-}" ]; then
  if [ -z "$PYTHON" ]; then
    echo "lab-tracker: python3 is required for password login; set LAB_TRACKER_API_KEY instead." >&2
    exit 1
  fi
  # Build the login body with json.dumps (never shell string interpolation) so a
  # username/password containing quotes or control characters cannot corrupt or
  # inject into the JSON, then parse the token structurally with json.
  login_body="$(LAB_TRACKER_ADMIN_USER="$LAB_TRACKER_ADMIN_USER" \
    LAB_TRACKER_ADMIN_PASS="${LAB_TRACKER_ADMIN_PASS:-}" \
    "$PYTHON" "$SCHED" login-body)"
  token="$(curl -fsS -X POST "$BASE_URL/auth/login" \
    -H 'Content-Type: application/json' \
    -d "$login_body" \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("data",{}).get("access_token",""))')"
  if [ -z "$token" ]; then
    echo "lab-tracker: could not obtain an admin token from $BASE_URL/auth/login" >&2
    exit 1
  fi
  auth_header="Authorization: Bearer $token"
fi

if [ -n "$auth_header" ]; then
  curl -fsS -X POST "$BASE_URL/batches/run-due" -H "$auth_header" >/dev/null
else
  curl -fsS -X POST "$BASE_URL/batches/run-due" >/dev/null
fi

echo "lab-tracker: daily review run-due triggered ($BASE_URL)"
