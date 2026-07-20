#!/usr/bin/env sh
# Run the MATLAB consumer smoke example end-to-end against a disposable Lab
# Tracker server.
#
# The MATLAB package (matlab/+labtracker) has source-contract tests, but a real
# capture can only be validated with a licensed MATLAB runtime, which neither
# this workstation nor CI has. So this runner is FAIL-SOFT-AWARE:
#
#   * If `matlab` is not on PATH it prints a skip notice and exits 0 (green), so
#     it is safe to wire into any pipeline without a MATLAB license.
#   * If `matlab` IS present it boots an ephemeral auth-disabled server on a temp
#     SQLite database, creates a project, runs matlab/examples/capture_figure_smoke.m
#     against it, and asserts the capture actually succeeded (result.action is
#     "imported" or "coalesced" — a skipped/failed capture must NOT pass as green,
#     because the MATLAB client is fail-soft by design).
#
# NOTE: the matlab-present path requires a MATLAB license and has not been
# executed on this workstation. See docs/lab-tracker-matlab.md for the manual,
# token-authenticated runbook.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EXAMPLE="$REPO_ROOT/matlab/examples/capture_figure_smoke.m"
PORT="${LAB_TRACKER_SMOKE_PORT:-8111}"
BASE_URL="http://127.0.0.1:$PORT"

if ! command -v matlab >/dev/null 2>&1; then
    echo "lab-tracker: matlab runtime not found on PATH — skipping the MATLAB smoke (this is not a failure)."
    echo "  Run this on a machine with a MATLAB license, or follow the manual runbook in docs/lab-tracker-matlab.md."
    exit 0
fi

PYTHON="${LAB_TRACKER_PYTHON:-$(command -v python3 || command -v python)}"
WORKDIR="$(mktemp -d)"
SERVER_PID=""
cleanup() {
    [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
    rm -rf "$WORKDIR"
}
trap cleanup EXIT INT TERM

export LAB_TRACKER_ENVIRONMENT="local"
export LAB_TRACKER_AUTH_ENABLED="false"
export LAB_TRACKER_DATABASE_URL="sqlite+pysqlite:///$WORKDIR/smoke.db"
export LAB_TRACKER_FILE_STORAGE_PATH="$WORKDIR/file-storage"
export LAB_TRACKER_NOTE_STORAGE_PATH="$WORKDIR/note-storage"

cd "$REPO_ROOT"
"$PYTHON" -m alembic upgrade head >/dev/null

"$PYTHON" -m uvicorn lab_tracker.asgi:app --host 127.0.0.1 --port "$PORT" >"$WORKDIR/server.log" 2>&1 &
SERVER_PID=$!

# Wait for the server to accept requests.
i=0
until curl -fsS "$BASE_URL/health" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -gt 60 ]; then
        echo "lab-tracker: server did not become ready; log:" >&2
        cat "$WORKDIR/server.log" >&2
        exit 1
    fi
    sleep 0.5
done

# Create a project to capture into (auth is disabled for this ephemeral server).
PROJECT_ID="$(curl -fsS -X POST "$BASE_URL/projects" \
    -H 'Content-Type: application/json' \
    -d '{"name":"MATLAB smoke"}' \
    | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["data"]["project_id"])')"

export LAB_TRACKER_BASE_URL="$BASE_URL"
export LAB_TRACKER_PROJECT_ID="$PROJECT_ID"
export LAB_TRACKER_ACCESS_TOKEN="smoke-noauth"  # ignored by the auth-disabled server

# Run the example and assert the capture succeeded. The MATLAB client is
# fail-soft (a misconfigured capture returns action="skipped"/"failed" rather
# than erroring), so we must inspect result.action explicitly.
matlab -batch "
try
    result = run('$EXAMPLE');
catch err
    fprintf(2, 'MATLAB error: %s\n', err.message); exit(1);
end
action = '';
if isstruct(result) && isfield(result, 'action'); action = char(string(result.action)); end
fprintf('LAB_TRACKER_SMOKE_ACTION=%s\n', action);
if ~(strcmp(action, 'imported') || strcmp(action, 'coalesced'));
    fprintf(2, 'MATLAB smoke did not import a figure (action=%s)\n', action); exit(2);
end
" | tee "$WORKDIR/matlab.out"

if grep -qE 'LAB_TRACKER_SMOKE_ACTION=(imported|coalesced)' "$WORKDIR/matlab.out"; then
    echo "lab-tracker: MATLAB smoke succeeded."
else
    echo "lab-tracker: MATLAB smoke FAILED (no imported/coalesced action)." >&2
    exit 1
fi
