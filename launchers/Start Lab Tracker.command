#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if command -v lab-tracker >/dev/null 2>&1; then
    exec lab-tracker serve
fi

if command -v uv >/dev/null 2>&1; then
    exec uv run lab-tracker serve
fi

if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python -m lab_tracker serve
fi

cat <<'EOF'
Lab Tracker is not installed in this checkout yet.

Install uv first, then double-click this launcher again:

  curl -LsSf https://astral.sh/uv/install.sh | sh

If macOS blocks this launcher the first time, right-click it, choose Open, then
confirm Open. Plain double-clicking works after that first approval.
EOF
printf '\nPress Return to close this window... '
read -r _
exit 1
