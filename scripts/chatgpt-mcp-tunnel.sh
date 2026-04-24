#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

HOST="${LAB_TRACKER_MCP_HOST:-127.0.0.1}"
PORT="${LAB_TRACKER_MCP_PORT:-8000}"
LOCAL_URL="http://${HOST}:${PORT}"
MCP_URL="${LOCAL_URL}/mcp"

export LAB_TRACKER_MCP_ACTOR_ROLE="${LAB_TRACKER_MCP_ACTOR_ROLE:-viewer}"
export LAB_TRACKER_MCP_ENABLE_WRITES="${LAB_TRACKER_MCP_ENABLE_WRITES:-false}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required. Install it with: brew install uv" >&2
  exit 1
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared is required for the one-command ChatGPT tunnel." >&2
  echo "Install it with: brew install cloudflared" >&2
  exit 1
fi

if ! [[ "${PORT}" =~ ^[0-9]+$ ]]; then
  echo "LAB_TRACKER_MCP_PORT must be a number." >&2
  exit 1
fi

if [[ ! -d ".venv" ]]; then
  uv venv
fi

uv pip install -q -e ".[mcp]"
uv run alembic upgrade head

uv run lab-tracker-mcp \
  --transport streamable-http \
  --host "${HOST}" \
  --port "${PORT}" &
server_pid=$!

cleanup() {
  kill "${server_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

python3 - <<PY
import socket
import sys
import time

host = "${HOST}"
port = ${PORT}
deadline = time.time() + 20
while time.time() < deadline:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex((host, port)) == 0:
            sys.exit(0)
    time.sleep(0.25)
raise SystemExit(f"Timed out waiting for MCP server at {host}:{port}")
PY

cat <<EOF

Lab Tracker MCP is running locally at:
  ${MCP_URL}

Starting a temporary Cloudflare tunnel.
When the https://*.trycloudflare.com URL appears below, use:
  https://<that-hostname>/mcp

Default MCP actor role: ${LAB_TRACKER_MCP_ACTOR_ROLE}
Writes enabled: ${LAB_TRACKER_MCP_ENABLE_WRITES}
For write testing, rerun with:
  LAB_TRACKER_MCP_ACTOR_ROLE=editor LAB_TRACKER_MCP_ENABLE_WRITES=true ./scripts/chatgpt-mcp-tunnel.sh

EOF

cloudflared tunnel --url "${LOCAL_URL}" 2>&1 | while IFS= read -r line; do
  echo "${line}"
  if [[ "${line}" =~ https://[-A-Za-z0-9.]+\.trycloudflare\.com ]]; then
    echo
    echo "ChatGPT MCP endpoint:"
    echo "  ${BASH_REMATCH[0]}/mcp"
    echo
  fi
done
