#!/bin/sh
set -eu

runtime_env_dir="${LAB_TRACKER_RUNTIME_ENV_DIR:-/app/data/runtime-env}"
auth_secret_file="${LAB_TRACKER_AUTH_SECRET_KEY_FILE:-${runtime_env_dir}/auth-secret-key}"
bootstrap_token_file="${LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_FILE:-${runtime_env_dir}/bootstrap-admin-token}"

generate_secret() {
    python - <<'PY'
import secrets

print(secrets.token_urlsafe(48))
PY
}

generate_bootstrap_token() {
    python - <<'PY'
import secrets

print(secrets.token_urlsafe(24))
PY
}

mkdir -p "$runtime_env_dir"

if [ -z "${LAB_TRACKER_PUBLIC_BASE_URL:-}" ] && [ -n "${RENDER_EXTERNAL_URL:-}" ]; then
    LAB_TRACKER_PUBLIC_BASE_URL="$RENDER_EXTERNAL_URL"
    export LAB_TRACKER_PUBLIC_BASE_URL
fi

if [ -z "${LAB_TRACKER_AUTH_SECRET_KEY:-}" ]; then
    if [ -s "$auth_secret_file" ]; then
        LAB_TRACKER_AUTH_SECRET_KEY="$(cat "$auth_secret_file")"
    else
        LAB_TRACKER_AUTH_SECRET_KEY="$(generate_secret)"
        umask 077
        printf '%s\n' "$LAB_TRACKER_AUTH_SECRET_KEY" > "$auth_secret_file"
        echo "Generated LAB_TRACKER_AUTH_SECRET_KEY in ${auth_secret_file}." >&2
    fi
    export LAB_TRACKER_AUTH_SECRET_KEY
fi

if [ -z "${LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN:-}" ]; then
    if [ -s "$bootstrap_token_file" ]; then
        LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN="$(cat "$bootstrap_token_file")"
    else
        LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN="$(generate_bootstrap_token)"
        umask 077
        printf '%s\n' "$LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN" > "$bootstrap_token_file"
        echo "Generated first-admin bootstrap token in ${bootstrap_token_file}." >&2
    fi
    export LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN
fi

echo "First admin setup token: ${LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN}" >&2

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
