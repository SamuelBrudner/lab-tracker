# Self-Hosted Operations

Use this for the Docker/Postgres path in `docker-compose.yml`.

## Data Locations

- Postgres data lives in the Docker volume `lab-tracker_postgres_data`.
- App files, note storage, generated auth secret, and generated bootstrap token
  live in the Docker volume `lab-tracker_app_data`.
- The app container runs `alembic upgrade head` on startup before serving.

Back up before updating the image or pulling new code because startup can run
schema migrations.

The optional hosted MCP service is behind the `mcp` Compose profile, so the
commands on this page never need `LT_MCP_INBOUND_TOKEN` or
`LT_MCP_READONLY_TOKEN`. When you run it, add `--profile mcp` (or set
`COMPOSE_PROFILES=mcp` in `.env`); see
[`deployment-options.md`](deployment-options.md#dockerpostgres-lab-instance).

## Reverse Proxy and Client Addresses

Uvicorn in the app container trusts `X-Forwarded-For` and `X-Forwarded-Proto`
only from peers listed in `FORWARDED_ALLOW_IPS`. The proxy must also send those
headers: Caddy's `reverse_proxy` does by default, while nginx needs
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` and
`proxy_set_header X-Forwarded-Proto $scheme;`. The compose default is
`127.0.0.1`, which matches no proxy outside the container, so forwarded headers
are ignored and every proxied request appears to come from the proxy's own
address. That address is private, so in the `local`
`LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE` mode a proxied internet client
would be treated as local until the proxy is trusted.

When a TLS reverse proxy (Caddy, nginx, or `tailscale serve`) on the Docker host
forwards to the published app port, the app sees the Compose network gateway
as its peer. Find it and set it in the ignored `.env` before creating the first
admin:

```bash
docker network inspect lab-tracker_default \
  --format '{{(index .IPAM.Config 0).Gateway}}'
```

The network name is `<project>_default`, where the Compose project name is the
checkout directory name unless `COMPOSE_PROJECT_NAME` (or `docker compose -p`)
sets it; `docker network ls` shows the actual name.

```dotenv
FORWARDED_ALLOW_IPS=172.18.0.1
```

For a proxy container on the same Compose network, use that container's
address instead. Recreate the app with `docker compose up -d app` after a
change. Never set `FORWARDED_ALLOW_IPS=*` while the app port is reachable
without going through the proxy: any client could then choose the address the
app sees.

Trusting the gateway is only safe when the proxy is the sole path to the app.
The root compose file publishes the app on every host interface
(`8000:8000`), so clients could still reach it directly, and Docker's userland
proxy relays some of those connections (IPv6 clients, Docker Desktop) from the
same gateway address, letting them forge `X-Forwarded-For`. When a proxy fronts
the app, publish the app port on loopback only with a local
`docker-compose.override.yml` next to `docker-compose.yml` (Compose loads it
automatically; keep it out of commits). `!override` replaces the published
ports instead of appending to them:

```yaml
services:
  app:
    ports: !override
      - "127.0.0.1:8000:8000"
```

A proxy container on the Compose network reaches `app:8000` directly and needs
no published port at all.

## Process Reaping

The image entrypoint runs under `tini`, which reaps orphaned grandchildren of
the bounded subprocesses used for registered Git and rclone stores. That covers
Render and plain `docker run` as well as Compose. The compose `app` and `mcp`
services also set `init: true`; `tini -s` then stays a child subreaper under
Docker's init. A custom `entrypoint:` bypasses `tini`, so keep `init: true` (or
`docker run --init`) wherever you override it, as the root `mcp` service does.

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

If you run the optional MCP service, rebuild and restart it from the same
checkout with `docker compose --profile mcp up -d --build mcp`.

If migrations fail after the configured retry budget, the app container exits
with an error. Restore from backup or fix the migration before restarting.

### Pinned provider-backed instances

A live instance that uses a reviewed immutable image must not be operated with
the source-build Compose file alone. Select the checked-in
`deployments/shared-provider/docker-compose.yml` overlay through the host's
ignored `.env`; the overlay removes the app and MCP build definitions and
requires both services to use `LAB_TRACKER_RELEASE_IMAGE`.

See [`deployments/shared-provider/README.md`](../deployments/shared-provider/README.md)
for the one-time host configuration and the fail-closed preflight. Once the
overlay is selected, ordinary `docker compose` commands retain the pinned image
and even `--build` cannot rebuild or retag it from the current checkout.

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
