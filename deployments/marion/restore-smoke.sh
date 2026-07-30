#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
repo_root="$(CDPATH= cd -- "${script_dir}/../.." && pwd)"
backup_root="${repo_root}/backups/marion"

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <backups/marion/TIMESTAMP directory>" >&2
  exit 2
fi

backup_dir="$(CDPATH= cd -- "$1" && pwd)"
case "${backup_dir}" in
  "${backup_root}"/*) ;;
  *)
    echo "Refusing a restore source outside ${backup_root}: ${backup_dir}" >&2
    exit 2
    ;;
esac

database_dump="${backup_dir}/marion-postgres.dump"
app_archive="${backup_dir}/marion-app-data.tar.gz"
test -f "${database_dump}"
test -f "${app_archive}"
docker run --rm \
  --volume "${backup_dir}:/backup:ro" \
  postgres:16-alpine \
  pg_restore --list /backup/marion-postgres.dump >/dev/null
tar -tzf "${app_archive}" >/dev/null

scratch_suffix="$(date '+%Y%m%d%H%M%S')-$$"
network="lab-tracker-marion-restore-${scratch_suffix}"
postgres_volume="lab-tracker-marion-restore-postgres-${scratch_suffix}"
app_volume="lab-tracker-marion-restore-app-${scratch_suffix}"
postgres_container="lab-tracker-marion-restore-postgres-${scratch_suffix}"
scratch_password="marion-restore-smoke-only"

cleanup() {
  docker rm -f "${postgres_container}" >/dev/null 2>&1 || true
  docker volume rm "${postgres_volume}" "${app_volume}" >/dev/null 2>&1 || true
  docker network rm "${network}" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker network create "${network}" >/dev/null
docker volume create "${postgres_volume}" >/dev/null
docker volume create "${app_volume}" >/dev/null
docker run --detach \
  --name "${postgres_container}" \
  --network "${network}" \
  --network-alias postgres \
  --env POSTGRES_DB=lab_tracker \
  --env POSTGRES_USER=lab_tracker \
  --env "POSTGRES_PASSWORD=${scratch_password}" \
  --volume "${postgres_volume}:/var/lib/postgresql/data" \
  postgres:16-alpine >/dev/null

attempt=0
until docker exec "${postgres_container}" \
  pg_isready -U lab_tracker -d lab_tracker >/dev/null 2>&1
do
  attempt=$((attempt + 1))
  if [ "${attempt}" -ge 30 ]; then
    echo "Scratch Postgres did not become ready." >&2
    exit 1
  fi
  sleep 1
done

docker run --rm \
  --network "${network}" \
  --env "PGPASSWORD=${scratch_password}" \
  --volume "${backup_dir}:/backup:ro" \
  postgres:16-alpine \
  pg_restore \
    --host postgres \
    --username lab_tracker \
    --dbname lab_tracker \
    --exit-on-error \
    /backup/marion-postgres.dump

restored_revision="$(
  docker exec \
    --env "PGPASSWORD=${scratch_password}" \
    "${postgres_container}" \
    psql \
      --username lab_tracker \
      --dbname lab_tracker \
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
      --username lab_tracker \
      --dbname lab_tracker \
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
    --username lab_tracker \
    --dbname lab_tracker \
    --set ON_ERROR_STOP=1 \
    --command "SELECT version_num FROM alembic_version;" \
    --command "SELECT count(*) AS restored_users FROM users;"

docker run --rm \
  --volume "${app_volume}:/restore" \
  --volume "${backup_dir}:/backup:ro" \
  alpine \
  tar -xzf /backup/marion-app-data.tar.gz -C /restore

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
