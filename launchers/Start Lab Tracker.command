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

exec python3 -m lab_tracker serve
