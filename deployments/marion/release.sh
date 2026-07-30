#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "${script_dir}/../.." && pwd)"
compose_file="${script_dir}/docker-compose.yml"
marion_env="${script_dir}/.env"
provider_env="${script_dir}/../shared-provider/.env"
public_health_url="https://lab-tracker.tail79f9d8.ts.net:8443/health"

for required_env in "${marion_env}" "${provider_env}"; do
  if [ ! -f "${required_env}" ]; then
    echo "Required deployment environment file is missing: ${required_env}" >&2
    exit 1
  fi
done

compose() {
  docker compose \
    --project-directory "${script_dir}" \
    --env-file "${marion_env}" \
    --env-file "${provider_env}" \
    -f "${compose_file}" \
    "$@"
}

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
release_image="lab-tracker-marion:sha-${revision}"

previous_container="$(
  docker ps \
    --filter label=com.docker.compose.project=lab-tracker-marion \
    --filter label=com.docker.compose.service=app \
    --quiet
)"
previous_image_id=""
previous_auth_secret_digest=""
if [ -n "${previous_container}" ]; then
  previous_image_id="$(
    docker inspect --format '{{.Image}}' "${previous_container}"
  )"
  previous_auth_secret_digest="$(
    docker exec "${previous_container}" \
      python -c \
      'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("/app/data/runtime-env/auth-secret-key").read_bytes()).hexdigest())'
  )"
  if [ -z "${previous_image_id}" ] || [ -z "${previous_auth_secret_digest}" ]; then
    echo "Could not capture the existing Marion image and auth-secret identity." >&2
    exit 1
  fi
fi

rollback_needed=0
rollback_on_exit() {
  status="$?"
  trap - 0 1 2 15
  if [ "${status}" -ne 0 ] &&
    [ "${rollback_needed}" -eq 1 ] &&
    [ -n "${previous_image_id}" ]
  then
    echo "Release failed after cutover; restoring the previous app image." >&2
    set +e
    MARION_IMAGE="${previous_image_id}"
    export MARION_IMAGE
    compose up -d --no-deps app
    rollback_status="$?"
    if [ "${rollback_status}" -eq 0 ]; then
      rollback_container="$(
        compose ps -q app
      )"
      rollback_attempt=0
      rollback_health=""
      while [ "${rollback_attempt}" -lt 30 ]; do
        rollback_health="$(
          docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
            "${rollback_container}"
        )"
        if [ "${rollback_health}" = "healthy" ]; then
          break
        fi
        rollback_attempt=$((rollback_attempt + 1))
        sleep 2
      done
      if [ "${rollback_health}" != "healthy" ]; then
        rollback_status=1
      fi
    fi
    if [ "${rollback_status}" -ne 0 ]; then
      echo "Automatic app-image rollback failed; use the validated backup at ${backup_dir:-unknown}." >&2
    else
      echo "Previous Marion app image is healthy again." >&2
    fi
  fi
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

MARION_IMAGE="${release_image}"
export MARION_IMAGE
rollback_needed=1
compose up -d

container_id="$(
  compose ps -q app
)"
if [ -z "${container_id}" ]; then
  echo "Marion app container was not created." >&2
  exit 1
fi

attempt=0
health_status=""
while [ "${attempt}" -lt 30 ]; do
  health_status="$(
    docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "${container_id}"
  )"
  if [ "${health_status}" = "healthy" ]; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "${health_status}" != "healthy" ]; then
  echo "Marion app did not become healthy (status: ${health_status:-unknown})." >&2
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
    echo "The persistent Marion auth-secret identity changed during release." >&2
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
    --expected-app-name marion-lab-tracker \
    --expected-environment production \
    --expected-source-revision "${revision}" \
    --require-provider-credential \
    --require-scheduler \
    --require-users \
    --require-admin \
    --alembic-config /app/alembic.ini

verify_health_identity() {
  health_url="$1"
  health_payload="$(
    curl \
      --fail \
      --silent \
      --show-error \
      --connect-timeout 5 \
      --max-time 15 \
      "${health_url}"
  )"
  printf '%s' "${health_payload}" |
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
' marion-lab-tracker production "${revision}"
}

verify_health_identity "http://127.0.0.1:8100/health"
verify_health_identity "${public_health_url}"

rollback_needed=0
compose ps
echo "Released Marion Lab Tracker ${source_version} at revision ${revision}."
