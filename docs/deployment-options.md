# Deployment Options

Lab Tracker can be used without each bench scientist touching a terminal, but
someone still needs to run or host the shared instance.

## One-Click Cloud Deploy

Use the Render Blueprint when a lab wants a managed shared instance and does not
want to maintain Docker or Postgres on a lab computer:

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/SamuelBrudner/lab-tracker)

The blueprint provisions the web service, managed Postgres database, persistent
file disk, generated auth secret, first-admin setup token, startup migrations,
TLS URL, restarts, and platform database backups. See
[`docs/one-click-cloud-deploy.md`](one-click-cloud-deploy.md).

## Current No-Uvicorn Paths

### Local Desktop Launcher

Use one of the files in `launchers/`:

- macOS: double-click `Start Lab Tracker.command`
- Windows: double-click `Start Lab Tracker.bat`

The launcher runs `lab-tracker serve`, which applies migrations, opens the
browser to `/app`, and starts the web server.

macOS first-run notes:

- Install `uv` before using `Start Lab Tracker.command`:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- If macOS Gatekeeper blocks the downloaded `.command` file, right-click
  `Start Lab Tracker.command`, choose `Open`, then confirm `Open`. After that,
  normal double-clicking works.

### Docker/Postgres Lab Instance

```bash
docker compose up app
```

On first boot, the container generates and persists:

- `LAB_TRACKER_AUTH_SECRET_KEY`
- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`

The first-admin token is stored in the app data volume. Open the app through
`http://127.0.0.1:8000/app` or another local/LAN/VPN host and choose
`Create First Admin`; the setup screen loads the generated token while no users
exist.

Optional GitHub Copilot MCP hosting is a separate read-only service:

```bash
LT_MCP_READONLY_TOKEN=lpat_... docker compose up mcp
```

The MCP service uses streamable HTTP and is published only on host loopback
(`127.0.0.1:9000` by default). Put a private TLS proxy or tailnet serve layer in
front of it; the checked-in Caddy example is `deploy/mcp/Caddyfile`.

### LAN Phone Capture

macOS/Linux:

```bash
scripts/serve-lan.sh --use-postgres
```

Windows:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres
```

See [`docs/phone-capture-quickstart.md`](phone-capture-quickstart.md).

## Hosted Read-Only Demo

The seeded read-only demo is live at
[`samuelbrudner.github.io/lab-tracker/app/`](https://samuelbrudner.github.io/lab-tracker/app/)
and linked from the repository homepage. It is the primary non-build preview
path; README screenshots remain the fallback when the hosted demo is
unavailable.

## Managed Lab Deployment

Managed lab deployments should keep infrastructure work out of the lab member's
terminal. The current repo supports first-admin setup, email invitation links,
global role grants, project membership grants, password reset, startup
migrations, Docker first-run secrets, and managed Render deployment with
platform database backups.
