#!/bin/sh
set -eu
umask 077

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "${script_dir}/../.." && pwd)"
timestamp="$(date '+%Y%m%d-%H%M%S-%Z')"
backup_root="${repo_root}/backups/marion"
backup_dir="${backup_root}/${timestamp}"
compose_file="${script_dir}/docker-compose.yml"
app_volume="lab-tracker-marion_app_data"
app_paused=0

resume_app() {
  if [ "${app_paused}" -eq 1 ]; then
    docker compose --project-directory "${script_dir}" -f "${compose_file}" \
      unpause app >/dev/null
    app_paused=0
  fi
}

resume_app_on_exit() {
  status="$?"
  trap - EXIT HUP INT TERM
  resume_app
  exit "${status}"
}
trap 'exit 1' HUP INT TERM
trap resume_app_on_exit EXIT

mkdir -p "${backup_root}"
chmod 700 "${repo_root}/backups" "${backup_root}"
# A timestamp collision must fail instead of overwriting a previously
# validated recovery point.
mkdir "${backup_dir}"
chmod 700 "${backup_dir}"

# Compose evaluates every service image even for `exec`. A backup never creates
# the app service, so use a harmless placeholder when no release image is
# exported rather than making recovery depend on the deploy shell's history.
MARION_IMAGE="${MARION_IMAGE:-lab-tracker-marion:backup-only}"
export MARION_IMAGE

docker compose --project-directory "${script_dir}" -f "${compose_file}" \
  pause app >/dev/null
app_paused=1

docker compose --project-directory "${script_dir}" -f "${compose_file}" exec -T postgres \
  pg_dump -U lab_tracker -d lab_tracker --format=custom \
  > "${backup_dir}/marion-postgres.dump"

: > "${backup_dir}/marion-app-data.tar.gz"
docker run --rm \
  -v "${app_volume}:/data:ro" \
  -v "${backup_dir}:/backup" \
  alpine tar -czf /backup/marion-app-data.tar.gz -C /data .

resume_app

docker run --rm \
  -v "${backup_dir}:/backup:ro" \
  postgres:16-alpine \
  pg_restore --list /backup/marion-postgres.dump >/dev/null

tar -tzf "${backup_dir}/marion-app-data.tar.gz" >/dev/null
chmod 600 \
  "${backup_dir}/marion-postgres.dump" \
  "${backup_dir}/marion-app-data.tar.gz"

echo "Validated Marion backup: ${backup_dir}"
shasum -a 256 \
  "${backup_dir}/marion-postgres.dump" \
  "${backup_dir}/marion-app-data.tar.gz"
echo "BACKUP_DIR=${backup_dir}"
