#!/usr/bin/env sh
set -eu

HOST="${LAB_TRACKER_HOST:-0.0.0.0}"
PORT="${LAB_TRACKER_PORT:-8000}"
USE_POSTGRES=0

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
        -h|--help)
            echo "Usage: scripts/serve-lan.sh [--use-postgres] [--host 0.0.0.0] [--port 8000]"
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

"$PYTHON_BIN" -m alembic upgrade head
exec "$PYTHON_BIN" -m uvicorn lab_tracker.asgi:app --host "$HOST" --port "$PORT"
