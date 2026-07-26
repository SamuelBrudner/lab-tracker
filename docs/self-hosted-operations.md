# Self-Hosted Operations

Use this for the Docker/Postgres path in `docker-compose.yml`.

## Data Locations

- Postgres data lives in the Docker volume `lab-tracker_postgres_data`.
- App files, note storage, generated auth secret, and generated bootstrap token
  live in the Docker volume `lab-tracker_app_data`.
- The app container runs `alembic upgrade head` on startup before serving.

Back up before updating the image or pulling new code because startup can run
schema migrations.

## Local Filesystem Stores

The normative local-root and mount contract is in
[`configuration.md`](configuration.md#mount-and-namespace-authority). An
allowed root grants the subtree visible in the app container's namespace,
including operator-installed POSIX ordinary and bind mounts; it does not grant
a stable device or volume identity.

Keep that namespace under deployment-operator control:

- mount configured roots read-only where the workflow permits it;
- do not give the app container `CAP_SYS_ADMIN`, host device-map control, or an
  untrusted FUSE or user-mount namespace;
- do not let API users or ordinary data writers replace mounts or Windows DOS
  device mappings beneath an allowed root; and
- quiesce filesystem operations and restart the app around planned mount,
  volume-map, or device-map changes.

If an untrusted principal can mutate that topology, disable local resolution
and local-store health or isolate the service in a namespace the principal
cannot change. Directory handles make one operation resistant to pathname
replacement; they are not a durable mount-topology lease.

## Backup

From the repo root:

```bash
mkdir -p backups
docker compose exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-lab_tracker}" \
  -d "${POSTGRES_DB:-lab_tracker}" \
  --format=custom \
  > "backups/lab-tracker-$(date +%Y%m%d-%H%M%S).dump"
```

Archive the app data volume:

```bash
docker run --rm \
  -v lab-tracker_app_data:/data:ro \
  -v "$PWD/backups:/backup" \
  alpine tar -czf /backup/lab-tracker-app-data.tar.gz -C /data .
```

## Restore

Stop the app before restoring:

```bash
docker compose stop app
```

Restore Postgres into an empty database:

```bash
cat backups/lab-tracker-YYYYMMDD-HHMMSS.dump | docker compose exec -T postgres \
  pg_restore \
  -U "${POSTGRES_USER:-lab_tracker}" \
  -d "${POSTGRES_DB:-lab_tracker}" \
  --clean \
  --if-exists
```

Restore app data:

```bash
docker run --rm \
  -v lab-tracker_app_data:/data \
  -v "$PWD/backups:/backup:ro" \
  alpine sh -c 'rm -rf /data/* && tar -xzf /backup/lab-tracker-app-data.tar.gz -C /data'
```

Start the app:

```bash
docker compose up -d app
```

## Upgrade

```bash
git pull --ff-only
docker compose build app
docker compose up -d app
docker compose logs -f app
```

If migrations fail after the configured retry budget, the app container exits
with an error. Restore from backup or fix the migration before restarting.

## First Admin Token

When no `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN` is provided, the Docker entrypoint
generates one and stores it in:

```text
/app/data/runtime-env/bootstrap-admin-token
```

Open the app through `http://127.0.0.1:8000/app` or another local/LAN/VPN host
and choose `Create First Admin`; the first-run setup screen loads the generated
token while no users exist. The token is not shown after the first user is
created. Public deployments can opt into browser display with
`LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE=first_run`; otherwise the token is
hidden on public hosts.
