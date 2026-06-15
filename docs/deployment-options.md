# Deployment Options

Lab Tracker can be used without each bench scientist touching a terminal, but
someone still needs to run or host the shared instance.

## Current No-Uvicorn Paths

### Local Desktop Launcher

Use one of the files in `launchers/`:

- macOS: double-click `Start Lab Tracker.command`
- Windows: double-click `Start Lab Tracker.bat`

The launcher runs `lab-tracker serve`, which applies migrations, opens the
browser to `/app`, and starts the web server.

### Docker/Postgres Lab Instance

```bash
docker compose up app
```

On first boot, the container generates and persists:

- `LAB_TRACKER_AUTH_SECRET_KEY`
- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`

The first-admin token is printed in `docker compose logs app` and stored in the
app data volume. Use it in the browser first-admin setup form.

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

## Hosted Demo Requirement

A public hosted demo should be seeded, resettable or read-only, and linked from
the repository homepage. Until that infrastructure is available, the README
screenshots are the non-build preview path.

## Managed Lab Requirement

A managed lab deployment should eventually handle email invites, password
recovery, backups, upgrades, and monitoring outside the lab member's terminal.
The current repo provides admin user management, first-admin setup, Docker
first-run secrets, and backup/restore documentation as local building blocks.
