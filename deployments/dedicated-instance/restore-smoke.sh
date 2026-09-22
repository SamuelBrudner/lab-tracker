#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(CDPATH= cd -- "${script_dir}/../.." && pwd -P)"
backup_root="${repo_root}/backups/dedicated-instance"

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <backups/dedicated-instance/PROJECT/TIMESTAMP directory>" >&2
  exit 2
fi

backup_dir="$(CDPATH= cd -- "$1" && pwd -P)"
case "${backup_dir}" in
  *.partial)
    echo "Refusing an incomplete backup directory: ${backup_dir}" >&2
    exit 2
    ;;
  "${backup_root}"/*/*) ;;
  *)
    echo "Refusing a restore source outside a project checkpoint under ${backup_root}: ${backup_dir}" >&2
    exit 2
    ;;
esac

database_dump="${backup_dir}/postgres.dump"
app_archive="${backup_dir}/app-data.tar.gz"
manifest="${backup_dir}/MANIFEST.sha256"
test -f "${database_dump}"
test -f "${app_archive}"
test -f "${manifest}"
if command -v sha256sum >/dev/null 2>&1; then
  (
    cd "${backup_dir}"
    sha256sum --check MANIFEST.sha256 >/dev/null
  )
elif command -v shasum >/dev/null 2>&1; then
  (
    cd "${backup_dir}"
    shasum -a 256 --check MANIFEST.sha256 >/dev/null
  )
else
  echo "A SHA-256 utility (sha256sum or shasum) is required." >&2
  exit 1
fi
docker run --rm \
  --volume "${backup_dir}:/backup:ro" \
  postgres:16-alpine \
  pg_restore --list /backup/postgres.dump >/dev/null
tar -tzf "${app_archive}" >/dev/null

scratch_suffix="$(date '+%Y%m%d%H%M%S')-$$"
network="lab-tracker-restore-${scratch_suffix}"
postgres_volume="lab-tracker-restore-postgres-${scratch_suffix}"
app_volume="lab-tracker-restore-app-${scratch_suffix}"
postgres_container="lab-tracker-restore-postgres-${scratch_suffix}"
scratch_database="restore_smoke_db"
scratch_user="restore_smoke_user"
scratch_password="restore-smoke-${scratch_suffix}"

cleanup() {
  if [ -n "${restore_log:-}" ]; then
    rm -f "${restore_log}"
  fi
  docker rm -f "${postgres_container}" >/dev/null 2>&1 || true
  docker volume rm "${postgres_volume}" "${app_volume}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
}
cleanup_on_exit() {
  status="$?"
  trap - 0 1 2 15
  cleanup
  exit "${status}"
}
trap 'exit 1' 1 2 15
trap cleanup_on_exit 0

docker network create "${network}" >/dev/null
docker volume create "${postgres_volume}" >/dev/null
docker volume create "${app_volume}" >/dev/null
docker run --detach \
  --name "${postgres_container}" \
  --network "${network}" \
  --network-alias postgres \
  --env "POSTGRES_DB=${scratch_database}" \
  --env "POSTGRES_USER=${scratch_user}" \
  --env "POSTGRES_PASSWORD=${scratch_password}" \
  --volume "${postgres_volume}:/var/lib/postgresql/data" \
  postgres:16-alpine >/dev/null

# The postgres image's first boot runs an init-only temporary server that
# listens on the Unix socket alone, then restarts on TCP. Probe over TCP from
# the scratch network -- the same path pg_restore uses -- so a socket-only
# answer from the temporary server cannot pass the readiness gate.
attempt=0
until docker run --rm \
  --network "${network}" \
  postgres:16-alpine \
  pg_isready \
    --host postgres \
    --username "${scratch_user}" \
    --dbname "${scratch_database}" >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  if [ "${attempt}" -ge 60 ]; then
    echo "Scratch Postgres did not accept TCP connections." >&2
    exit 1
  fi
  sleep 1
done

# Retry only connection-level failures (for example a restart gap right after
# the readiness probe); any other pg_restore error fails the smoke at once.
restore_log="$(mktemp)"
restore_attempt=0
restore_max_attempts=5
until docker run --rm \
  --network "${network}" \
  --env "PGPASSWORD=${scratch_password}" \
  --volume "${backup_dir}:/backup:ro" \
  postgres:16-alpine \
  pg_restore \
    --host postgres \
    --username "${scratch_user}" \
    --dbname "${scratch_database}" \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    /backup/postgres.dump 2>"${restore_log}"
do
  cat "${restore_log}" >&2
  restore_attempt=$((restore_attempt + 1))
  if ! grep -E -q 'connection to server|could not connect|Connection refused|the database system is (starting up|shutting down)' "${restore_log}"; then
    echo "pg_restore failed; not retrying a non-connection error." >&2
    exit 1
  fi
  if [ "${restore_attempt}" -ge "${restore_max_attempts}" ]; then
    echo "pg_restore could not connect to scratch Postgres after ${restore_attempt} attempts." >&2
    exit 1
  fi
  sleep 2
done
cat "${restore_log}" >&2

restored_revision="$(
  docker exec \
    --env "PGPASSWORD=${scratch_password}" \
    "${postgres_container}" \
    psql \
      --username "${scratch_user}" \
      --dbname "${scratch_database}" \
      --tuples-only \
      --no-align \
      --set ON_ERROR_STOP=1 \
      --command "SELECT version_num FROM alembic_version;"
)"
restored_users="$(
  docker exec \
    --env "PGPASSWORD=${scratch_password}" \
    "${postgres_container}" \
    psql \
      --username "${scratch_user}" \
      --dbname "${scratch_database}" \
      --tuples-only \
      --no-align \
      --set ON_ERROR_STOP=1 \
      --command "SELECT count(*) FROM users;"
)"
if [ -z "${restored_revision}" ]; then
  echo "Restored database has no migration revision." >&2
  exit 1
fi
case "${restored_users}" in
  ""|*[!0-9]*)
    echo "Restored database returned an invalid user count." >&2
    exit 1
    ;;
esac
if [ "${restored_users}" -lt 1 ]; then
  echo "Restored database has no users; refusing a possible wrong-instance backup." >&2
  exit 1
fi

docker exec \
  --env "PGPASSWORD=${scratch_password}" \
  "${postgres_container}" \
  psql \
    --username "${scratch_user}" \
    --dbname "${scratch_database}" \
    --set ON_ERROR_STOP=1 \
    --command "SELECT version_num FROM alembic_version;" \
    --command "SELECT count(*) AS restored_users FROM users;"

docker run --rm \
  --volume "${app_volume}:/restore" \
  --volume "${backup_dir}:/backup:ro" \
  alpine \
  tar -xzf /backup/app-data.tar.gz -C /restore

restored_entries="$(
  docker run --rm \
    --volume "${app_volume}:/restore:ro" \
    alpine \
    find /restore -mindepth 1 -print |
    wc -l |
    tr -d ' '
)"
if [ "${restored_entries}" -lt 1 ]; then
  echo "Restored app-data volume is unexpectedly empty." >&2
  exit 1
fi

echo "Disposable restore succeeded for ${backup_dir} (${restored_entries} app-data entries)."
