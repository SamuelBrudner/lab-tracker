#!/usr/bin/env sh
# Trigger one pass of Lab Tracker's daily review.
#
# POSTs /batches/run-due, which drafts over staged notes for every project whose
# batch settings are enabled and due. This only ever proposes a draft for human
# review -- it never commits anything to your graph.
#
# Configuration (all optional):
#   LAB_TRACKER_BASE_URL    API base URL          (default http://127.0.0.1:8000)
#   LAB_TRACKER_ADMIN_USER  admin username        (only when auth is enabled)
#   LAB_TRACKER_ADMIN_PASS  admin password        (only when auth is enabled)
#
# Exit non-zero if the API is unreachable or login fails.
set -eu

BASE_URL="${LAB_TRACKER_BASE_URL:-http://127.0.0.1:8000}"

auth_header=""
if [ -n "${LAB_TRACKER_ADMIN_USER:-}" ]; then
  # Mint a fresh short-lived admin token each run (tokens expire), no jq needed.
  token="$(curl -fsS -X POST "$BASE_URL/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${LAB_TRACKER_ADMIN_USER}\",\"password\":\"${LAB_TRACKER_ADMIN_PASS:-}\"}" \
    | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
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
