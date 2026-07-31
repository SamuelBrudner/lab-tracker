#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(CDPATH= cd -- "${script_dir}/../.." && pwd -P)"
. "${script_dir}/common.sh"

require_deployment_envs
compose_project_name="$(deployment_value DEDICATED_COMPOSE_PROJECT_NAME)"
app_name="$(deployment_value DEDICATED_APP_NAME)"
public_base_url="$(deployment_value DEDICATED_PUBLIC_BASE_URL)"
host_port="$(deployment_value DEDICATED_HOST_PORT)"
health_attempts="$(deployment_value DEDICATED_RELEASE_HEALTH_ATTEMPTS)"
health_interval="$(deployment_value DEDICATED_RELEASE_HEALTH_INTERVAL_SECONDS)"
connect_timeout="$(deployment_value DEDICATED_RELEASE_CONNECT_TIMEOUT_SECONDS)"
request_timeout="$(deployment_value DEDICATED_RELEASE_MAX_TIME_SECONDS)"
database_name="$(deployment_value DEDICATED_POSTGRES_DB)"
database_user="$(deployment_value DEDICATED_POSTGRES_USER)"
database_password="$(deployment_value DEDICATED_POSTGRES_PASSWORD)"
provider_credential="$(provider_value DEDICATED_OPENAI_API_KEY)"

require_compose_project_name "${compose_project_name}"
require_positive_integer DEDICATED_HOST_PORT "${host_port}"
require_positive_integer DEDICATED_RELEASE_HEALTH_ATTEMPTS "${health_attempts}"
require_positive_integer DEDICATED_RELEASE_HEALTH_INTERVAL_SECONDS "${health_interval}"
require_positive_integer DEDICATED_RELEASE_CONNECT_TIMEOUT_SECONDS "${connect_timeout}"
require_positive_integer DEDICATED_RELEASE_MAX_TIME_SECONDS "${request_timeout}"
if [ "${host_port}" -gt 65535 ]; then
  echo "DEDICATED_HOST_PORT must not exceed 65535." >&2
  exit 1
fi
case "${public_base_url}" in
  https://*) ;;
  *)
    echo "DEDICATED_PUBLIC_BASE_URL must be an HTTPS URL." >&2
    exit 1
    ;;
esac
case "${public_base_url}" in
  *.example.org|*.example.org:*)
    echo "DEDICATED_PUBLIC_BASE_URL still contains the reserved example hostname." >&2
    exit 1
    ;;
esac
for database_component in "${database_name}" "${database_user}" "${database_password}"; do
  case "${database_component}" in
    ""|*[!A-Za-z0-9._~-]*)
      echo "Database URL components must use unescaped URL-safe characters." >&2
      exit 1
      ;;
  esac
done
case "${database_password}" in
  replace-with-*)
    echo "DEDICATED_POSTGRES_PASSWORD still contains the example placeholder." >&2
    exit 1
    ;;
esac
case "${provider_credential}" in
  replace-with-*)
    echo "The provider credential file still contains the example placeholder." >&2
    exit 1
    ;;
esac
unset database_component database_password provider_credential lt_env_value
case "${public_base_url}" in
  *[[:space:]]*|*/)
    echo "DEDICATED_PUBLIC_BASE_URL must not contain whitespace or end with a slash." >&2
    exit 1
    ;;
esac

if [ -n "$(git -C "${repo_root}" status --porcelain)" ]; then
  echo "Refusing to release a dirty worktree." >&2
  exit 1
fi

revision="$(git -C "${repo_root}" rev-parse HEAD)"
if [ "${#revision}" -ne 40 ]; then
  echo "Expected a full 40-character Git revision, received: ${revision}" >&2
  exit 1
fi
case "${revision}" in
  *[!0-9a-f]*)
    echo "Git revision contains unexpected characters: ${revision}" >&2
    exit 1
    ;;
esac

upstream="$(
  git -C "${repo_root}" rev-parse \
    --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null || true
)"
upstream_revision=""
if [ -n "${upstream}" ]; then
  upstream_revision="$(git -C "${repo_root}" rev-parse "${upstream}")"
fi
if [ "${upstream_revision}" != "${revision}" ]; then
  echo "Refusing to release a revision that is not the pushed upstream tip." >&2
  exit 1
fi

source_version="$(
  sed -n 's/^version = "\([^"]*\)"$/\1/p' "${repo_root}/pyproject.toml"
)"
if [ -z "${source_version}" ]; then
  echo "Unable to read the project version from pyproject.toml." >&2
  exit 1
fi
release_image="${compose_project_name}-app:sha-${revision}"

release_lock="${TMPDIR:-/tmp}/lab-tracker-${compose_project_name}-release.lock"
release_lock_held=0
cleanup_release_lock() {
  if [ "${release_lock_held}" -eq 1 ]; then
    if ! rmdir "${release_lock}"; then
      echo "Release lock could not be removed: ${release_lock}" >&2
    fi
    release_lock_held=0
  fi
}
lock_only_on_exit() {
  status="$?"
  trap - 0 1 2 15
  cleanup_release_lock
  exit "${status}"
}
if ! mkdir -m 700 "${release_lock}" 2>/dev/null; then
  echo "Another release may be active; lock already exists: ${release_lock}" >&2
  exit 1
fi
release_lock_held=1
trap 'exit 1' 1 2 15
trap lock_only_on_exit 0

previous_containers="$(
  docker ps --all \
    --filter "label=com.docker.compose.project=${compose_project_name}" \
    --filter label=com.docker.compose.service=app \
    --format '{{.ID}}'
)"
previous_container_count="$(
  printf '%s\n' "${previous_containers}" |
    awk 'NF { count += 1 } END { print count + 0 }'
)"
if [ "${previous_container_count}" -ne 1 ]; then
  echo "Expected exactly one existing app container for ${compose_project_name}; found ${previous_container_count}." >&2
  exit 1
fi
previous_container="${previous_containers}"
previous_container_running="$(
  docker inspect --format '{{.State.Running}}' "${previous_container}"
)"
if [ "${previous_container_running}" != "true" ]; then
  echo "The existing app container is stopped; start and verify it before release." >&2
  exit 1
fi
previous_image_id=""
previous_auth_secret_digest=""
previous_image_id="$(
  docker inspect --format '{{.Image}}' "${previous_container}"
)"
previous_auth_secret_digest="$(
  docker exec "${previous_container}" \
    python -c \
    'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/app/data/runtime-env/auth-secret-key").read_bytes()).hexdigest())'
)"
if [ -z "${previous_image_id}" ] || [ -z "${previous_auth_secret_digest}" ]; then
  echo "Could not capture the existing app image and auth-secret identity." >&2
  exit 1
fi

rollback_needed=0
rollback_on_exit() {
  status="$?"
  trap - 0 1 2 15
  if [ "${status}" -ne 0 ] && [ "${rollback_needed}" -eq 1 ]; then
    set +e
    if [ -n "${previous_image_id}" ]; then
      echo "Release failed after cutover; restoring the previous immutable app image." >&2
      DEDICATED_IMAGE="${previous_image_id}"
      export DEDICATED_IMAGE
      compose up -d --no-deps app
      rollback_status="$?"
      if [ "${rollback_status}" -eq 0 ]; then
        rollback_container="$(compose ps -q app)"
        if [ -z "${rollback_container}" ]; then
          rollback_status=1
        fi
      fi
      if [ "${rollback_status}" -eq 0 ]; then
        rollback_attempt=0
        rollback_health=""
        while [ "${rollback_attempt}" -lt "${health_attempts}" ]; do
          rollback_health="$(
            docker inspect \
              --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
              "${rollback_container}"
          )"
          if [ "${rollback_health}" = "healthy" ]; then
            break
          fi
          rollback_attempt=$((rollback_attempt + 1))
          sleep "${health_interval}"
        done
        if [ "${rollback_health}" != "healthy" ]; then
          rollback_status=1
        fi
      fi
      if [ "${rollback_status}" -ne 0 ]; then
        echo "Automatic app-image rollback failed; use the validated backup at ${backup_dir:-unknown}." >&2
      else
        echo "The previous immutable app image is healthy again." >&2
      fi
    fi
  fi
  cleanup_release_lock
  exit "${status}"
}
trap 'exit 1' 1 2 15
trap rollback_on_exit 0

backup_output="$("${script_dir}/backup.sh")"
printf '%s\n' "${backup_output}"
backup_dir="$(
  printf '%s\n' "${backup_output}" |
    sed -n 's/^BACKUP_DIR=//p'
)"
if [ -z "${backup_dir}" ]; then
  echo "Backup did not report its validated output directory." >&2
  exit 1
fi
"${script_dir}/restore-smoke.sh" "${backup_dir}"

git -C "${repo_root}" archive --format=tar "${revision}" |
  docker build \
    --build-arg "LAB_TRACKER_SOURCE_REVISION=${revision}" \
    --build-arg "LAB_TRACKER_SOURCE_VERSION=${source_version}" \
    --tag "${release_image}" \
    -

image_revision="$(
  docker image inspect "${release_image}" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
)"
if [ "${image_revision}" != "${revision}" ]; then
  echo "Image revision mismatch: expected ${revision}, received ${image_revision}" >&2
  exit 1
fi

DEDICATED_IMAGE="${release_image}"
export DEDICATED_IMAGE
rollback_needed=1
compose up -d --no-deps app

container_id="$(compose ps -q app)"
if [ -z "${container_id}" ]; then
  echo "The dedicated app container was not created." >&2
  exit 1
fi

attempt=0
health_status=""
while [ "${attempt}" -lt "${health_attempts}" ]; do
  health_status="$(
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "${container_id}"
  )"
  if [ "${health_status}" = "healthy" ]; then
    break
  fi
  attempt=$((attempt + 1))
  sleep "${health_interval}"
done
if [ "${health_status}" != "healthy" ]; then
  echo "The dedicated app did not become healthy (status: ${health_status:-unknown})." >&2
  exit 1
fi

container_revision="$(
  docker inspect \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
    "${container_id}"
)"
if [ "${container_revision}" != "${revision}" ]; then
  echo "Running container revision mismatch: ${container_revision}" >&2
  exit 1
fi

if [ -n "${previous_auth_secret_digest}" ]; then
  current_auth_secret_digest="$(
    docker exec "${container_id}" \
      python -c \
      'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/app/data/runtime-env/auth-secret-key").read_bytes()).hexdigest())'
  )"
  if [ "${current_auth_secret_digest}" != "${previous_auth_secret_digest}" ]; then
    echo "The persistent auth-secret identity changed during release." >&2
    exit 1
  fi
fi

docker exec "${container_id}" \
  sh -eu -c '
    auth_secret_file="${LAB_TRACKER_AUTH_SECRET_KEY_FILE:-/app/data/runtime-env/auth-secret-key}"
    test -s "${auth_secret_file}"
    LAB_TRACKER_AUTH_SECRET_KEY="$(cat "${auth_secret_file}")"
    export LAB_TRACKER_AUTH_SECRET_KEY
    exec python -m lab_tracker.deployment_probe "$@"
  ' sh \
    --expected-app-name "${app_name}" \
    --expected-environment production \
    --expected-source-revision "${revision}" \
    --require-provider-credential \
    --require-scheduler \
    --require-users \
    --require-admin \
    --alembic-config /app/alembic.ini

verify_health_identity() {
  health_url="$1"
  if ! health_payload="$(
    curl \
      --fail \
      --silent \
      --show-error \
      --connect-timeout "${connect_timeout}" \
      --max-time "${request_timeout}" \
      "${health_url}"
  )"; then
    return 1
  fi
  if ! printf '%s' "${health_payload}" |
    python3 -c '
import json
import sys

expected_name, expected_environment, expected_revision = sys.argv[1:]
payload = json.load(sys.stdin)
app = payload.get("app") if isinstance(payload, dict) else None
valid = (
    payload.get("status") == "ok"
    and isinstance(app, dict)
    and app.get("name") == expected_name
    and app.get("environment") == expected_environment
    and app.get("source_revision") == expected_revision
)
raise SystemExit(0 if valid else 1)
' "${app_name}" production "${revision}"
  then
    return 1
  fi
}

verify_health_with_retries() {
  health_url="$1"
  health_attempt=1
  while [ "${health_attempt}" -le "${health_attempts}" ]; do
    if verify_health_identity "${health_url}"; then
      return 0
    fi
    if [ "${health_attempt}" -lt "${health_attempts}" ]; then
      sleep "${health_interval}"
    fi
    health_attempt=$((health_attempt + 1))
  done
  echo "Deployment identity check failed after ${health_attempts} attempts: ${health_url}" >&2
  return 1
}

verify_health_with_retries "http://127.0.0.1:${host_port}/health"
verify_health_with_retries "${public_base_url}/health"

rollback_needed=0
compose ps
echo "Released dedicated Lab Tracker ${source_version} at revision ${revision}."
