#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(dirname "$0")
SCRIPT_DIR=$(CDPATH= cd "$SCRIPT_DIR" && pwd)
REPO_ROOT=$(CDPATH= cd "${SCRIPT_DIR}/.." && pwd)
cd "$REPO_ROOT"
export PYTHONPATH="${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"

HOST="${LAB_TRACKER_HOST:-0.0.0.0}"
PORT="${LAB_TRACKER_PORT:-8000}"
USE_POSTGRES=0
ALLOW_INSECURE_AUTH_DISABLED=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --use-postgres)
            USE_POSTGRES=1
            ;;
        --host)
            shift
            HOST="$1"
            ;;
        --port)
            shift
            PORT="$1"
            ;;
        --allow-insecure-auth-disabled)
            ALLOW_INSECURE_AUTH_DISABLED=1
            ;;
        -h|--help)
            echo "Usage: scripts/serve-lan.sh [--use-postgres] [--host 0.0.0.0] [--port 8000] [--allow-insecure-auth-disabled]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

if [ "$USE_POSTGRES" -eq 1 ]; then
    export LAB_TRACKER_DATABASE_URL="${LAB_TRACKER_DATABASE_URL:-postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker}"
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
fi

LAN_IP="$("$PYTHON_BIN" - <<'PY'
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    sock.connect(("8.8.8.8", 80))
    print(sock.getsockname()[0])
except OSError:
    print(socket.gethostbyname(socket.gethostname()))
finally:
    sock.close()
PY
)"

APP_URL="http://${LAN_IP}:${PORT}/app"
CAPTURE_URL="http://${LAN_IP}:${PORT}/app/capture"

echo "Lab Tracker LAN URL: ${APP_URL}"
echo "Phone capture URL: ${CAPTURE_URL}"
echo ""
"$PYTHON_BIN" - "$CAPTURE_URL" <<'PY' || true
import sys

try:
    import segno
except ImportError:
    print("Install the segno Python package to print a QR code.")
    raise SystemExit(0)

segno.make(sys.argv[1]).terminal(compact=True)
PY

if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
    AUTH_ENABLED="$("$PYTHON_BIN" - <<'PY'
from lab_tracker.config import get_settings

print("true" if get_settings().is_auth_enabled() else "false")
PY
)"
    case "${LAB_TRACKER_ALLOW_INSECURE_AUTH_DISABLED:-}" in
        1|true|TRUE|yes|YES)
            ALLOW_INSECURE_AUTH_DISABLED=1
            ;;
    esac
    if [ "$AUTH_ENABLED" != "true" ] && [ "$ALLOW_INSECURE_AUTH_DISABLED" -ne 1 ]; then
        echo "Refusing to bind Lab Tracker to ${HOST} while authentication is disabled." >&2
        echo "Set LAB_TRACKER_AUTH_ENABLED=true and LAB_TRACKER_AUTH_SECRET_KEY, or pass --allow-insecure-auth-disabled only for a trusted temporary LAN." >&2
        exit 1
    fi
fi

"$PYTHON_BIN" -m alembic upgrade head
exec "$PYTHON_BIN" -m uvicorn lab_tracker.asgi:app --host "$HOST" --port "$PORT"
