#!/bin/sh
set -eu
umask 077

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(CDPATH= cd -- "${script_dir}/../.." && pwd -P)"
. "${script_dir}/common.sh"

require_deployment_envs
compose_project_name="$(deployment_value DEDICATED_COMPOSE_PROJECT_NAME)"
require_compose_project_name "${compose_project_name}"

timestamp="$(date '+%Y%m%d-%H%M%S-%Z')"
backup_root="${repo_root}/backups/dedicated-instance"
project_backup_root="${backup_root}/${compose_project_name}"
backup_dir="${project_backup_root}/${timestamp}"
staging_dir="${backup_dir}.partial"
app_paused=0

resume_app() {
  if [ "${app_paused}" -eq 1 ]; then
    compose unpause app >/dev/null
    app_paused=0
  fi
}

resume_app_on_exit() {
  status="$?"
  trap - 0 1 2 15
  resume_app
  exit "${status}"
}
trap 'exit 1' 1 2 15
trap resume_app_on_exit 0

mkdir -p "${project_backup_root}"
chmod 700 \
  "${repo_root}/backups" \
  "${backup_root}" \
  "${project_backup_root}"
# A timestamp collision must fail instead of overwriting an existing checkpoint.
# Incomplete work remains visibly suffixed with .partial and is never accepted
# by restore-smoke.sh.
test ! -e "${backup_dir}"
mkdir "${staging_dir}"
chmod 700 "${staging_dir}"

# Compose evaluates the app image for commands against other services. Backups
# never create the app, so provide an inert placeholder when release.sh has not
# exported a reviewed image.
DEDICATED_IMAGE="${DEDICATED_IMAGE:-lab-tracker-dedicated:backup-only}"
export DEDICATED_IMAGE

app_container="$(compose ps -q app)"
if [ -z "${app_container}" ]; then
  echo "The dedicated app container is not running; no coherent checkpoint can be taken." >&2
  exit 1
fi
case "${app_container}" in
  *'
'*)
    echo "Expected exactly one dedicated app container." >&2
    exit 1
    ;;
esac
app_volume="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/app/data"}}{{.Name}}{{end}}{{end}}' \
    "${app_container}"
)"
if [ -z "${app_volume}" ]; then
  echo "The app data mount is not a named Docker volume." >&2
  exit 1
fi
postgres_container="$(compose ps -q postgres)"
if [ -z "${postgres_container}" ]; then
  echo "The dedicated Postgres container is not running." >&2
  exit 1
fi
postgres_volume="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
    "${postgres_container}"
)"
if [ -z "${postgres_volume}" ]; then
  echo "The Postgres data mount is not a named Docker volume." >&2
  exit 1
fi

# Pause the only application writer until both the database dump and app-data
# archive have been captured. This makes the pair one maintenance checkpoint.
compose pause app >/dev/null
app_paused=1

compose exec -T postgres \
  sh -eu -c 'exec pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --format=custom' \
  > "${staging_dir}/postgres.dump"

: > "${staging_dir}/app-data.tar.gz"
docker run --rm \
  -v "${app_volume}:/data:ro" \
  -v "${staging_dir}:/backup" \
  alpine \
  tar -czf /backup/app-data.tar.gz -C /data .

resume_app

docker run --rm \
  -v "${staging_dir}:/backup:ro" \
  postgres:16-alpine \
  pg_restore --list /backup/postgres.dump >/dev/null
tar -tzf "${staging_dir}/app-data.tar.gz" >/dev/null
chmod 600 \
  "${staging_dir}/postgres.dump" \
  "${staging_dir}/app-data.tar.gz"

if command -v sha256sum >/dev/null 2>&1; then
  (
    cd "${staging_dir}"
    sha256sum postgres.dump app-data.tar.gz > MANIFEST.sha256
    sha256sum --check MANIFEST.sha256 >/dev/null
  )
elif command -v shasum >/dev/null 2>&1; then
  (
    cd "${staging_dir}"
    shasum -a 256 postgres.dump app-data.tar.gz > MANIFEST.sha256
    shasum -a 256 --check MANIFEST.sha256 >/dev/null
  )
else
  echo "A SHA-256 utility (sha256sum or shasum) is required." >&2
  exit 1
fi
chmod 600 "${staging_dir}/MANIFEST.sha256"
mv "${staging_dir}" "${backup_dir}"

echo "Validated dedicated-instance backup: ${backup_dir}"
cat "${backup_dir}/MANIFEST.sha256"
echo "BACKUP_DIR=${backup_dir}"
