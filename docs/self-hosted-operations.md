# Self-Hosted Operations

Use this for the Docker/Postgres path in `docker-compose.yml`.

## Data Locations

- Postgres data lives in the Docker volume `lab-tracker_postgres_data`.
- App files, note storage, generated auth secret, and generated bootstrap token
  live in the Docker volume `lab-tracker_app_data`.
- The app container runs `alembic upgrade head` on startup before serving.

Back up before updating the image or pulling new code because startup can run
schema migrations.

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

The token is also printed in `docker compose logs app` so the first admin can be
created from the browser.
