#!/bin/sh
set -eu

max_attempts="${MIGRATION_MAX_ATTEMPTS:-30}"
sleep_seconds="${MIGRATION_SLEEP_SECONDS:-2}"
attempt=1

until alembic upgrade head; do
    if [ "$attempt" -ge "$max_attempts" ]; then
        echo "Migration failed after ${attempt} attempts." >&2
        exit 1
    fi
    echo "Migration attempt ${attempt} failed; retrying in ${sleep_seconds}s..." >&2
    attempt=$((attempt + 1))
    sleep "$sleep_seconds"
done

exec "$@"
