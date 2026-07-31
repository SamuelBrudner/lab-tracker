# Install and Run Lab Tracker

This is the full local setup guide: prerequisites, install, running the API,
the frontend build, database migrations, first-admin setup, and validation. It
is for the lab member or IT contact who installs software. Bench scientists who
just need to *use* an instance their lab already runs do not need this page —
open the link your admin gave you and sign in.

The supported runtime surface is defined in
[`retained-v1-surface.md`](retained-v1-surface.md); if it and this guide
disagree, the retained-surface document wins.

## Contents

- [Prerequisites and install](#prerequisites-and-install)
- [Run the API](#run-the-api)
- [Multi-client Postgres runtime](#multi-client-postgres-runtime)
- [Serve on a LAN or VPN](#serve-on-a-lan-or-vpn)
- [Frontend build](#frontend-build)
- [Database migrations](#database-migrations)
- [First-admin setup](#first-admin-setup)
- [Validation](#validation)
- [Related docs](#related-docs)

## Prerequisites and install

### With uv (recommended)

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[test,lint]"
```

Install `uv` first if needed (for example: `brew install uv` or `pipx install uv`).

### With pip and venv (fallback)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test,lint]"
```

Commands below use `uv run`. If you used pip/venv instead, drop the `uv run` prefix.

The `[test,lint]` extras pull in the backend test and lint tooling. To capture
Matplotlib figures with the Python client (`lab_tracker_client.savefig`,
`capture_figures`), also install the `figure` extra, which adds `matplotlib`
and `pillow`:

```bash
uv pip install -e ".[test,lint,figure]"
```

Windows fresh-clone notes, including Beads/Dolt setup, are in
[`windows-fresh-clone.md`](windows-fresh-clone.md).

## Run the API

### Preferred local launcher

```bash
lab-tracker serve
```

That command runs `alembic upgrade head`, opens `http://127.0.0.1:8000/app`,
and starts the server. When the configured database is file-backed SQLite, it
first writes a migration-safety snapshot to `LAB_TRACKER_BACKUP_PATH`
(`~/.lab-tracker/backups` by default).

### Double-click launchers

Double-click launchers are available in `deploy/launchers/` for macOS and Windows:

- macOS: `Start Lab Tracker.command`
- Windows: `Start Lab Tracker.bat`

macOS launcher notes:

- Install `uv` before using `Start Lab Tracker.command`:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- If macOS Gatekeeper blocks the downloaded `.command` file the first time,
  right-click `Start Lab Tracker.command`, choose `Open`, then confirm `Open`.
  After that, normal double-clicking works.

The launcher path is also covered in
[`deployment-options.md`](deployment-options.md).

### Developer fallback

```bash
uv run uvicorn lab_tracker.asgi:app --reload
```

### Verify it is running

Health check:

```bash
curl http://127.0.0.1:8000/health
```

Then open the app at `http://127.0.0.1:8000/app`.

### Seed demo data

To populate the configured database with a local-development demo project:

```bash
lab-tracker seed-demo
```

It runs migrations first (skip with `--skip-migrations`) and is a no-op if the
default demo project already exists (force a fresh one with `--allow-duplicates`).
This is the same seeded data behind the read-only public demo.

### Check managed idiom blocks

`lab-tracker doctor` (alias `check-idioms`) checks the package-pinned,
code-facing idiom blocks in a consumer repo for drift against the installed
package. Pass `--target <path>` to inspect a repo other than the current
directory.

### Update a consumer repo after upgrading

`lt update` (equivalently `lab-tracker update`) refreshes a previously
initialised consumer repo to the installed package version in one step:
managed prompt blocks are re-rendered in place (your original consent choice
is preserved; add missing conventions blocks with `--yes`), and scaffolded
integration files — the `.claude/settings.json` prompt hook, `.mcp.json`,
`.cursor/mcp.json`, the `scripts/lt.py` shim, and `AGENTS.lt.md` — are
rewritten to the current canonical text. A file whose content differs is
first preserved next to itself as `*.bak-lt-update`, and `lt_ids.json` is
never touched. `--dry-run` previews the changes; run `lt doctor` afterwards
to confirm the repo is in sync.

## Multi-client Postgres runtime

For browser, Codex, Claude, scripts, and future workers writing at the same time,
use Postgres as the live source of truth and keep writes behind the Lab Tracker
API. SQLite remains the default single-client local fallback.

Start only Postgres for local development:

```powershell
docker compose up postgres
$env:LAB_TRACKER_DATABASE_URL = "postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker"
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Or run the full app stack:

```bash
docker compose up app
```

On first boot, the app container generates a persistent auth secret and first
admin bootstrap token if you did not set them. See
[First-admin setup](#first-admin-setup) below.

The full multi-client workflow — including running Postgres as the shared
source of truth for several machines — is documented in
[`lan-shared-graph.md`](lan-shared-graph.md).

## Serve on a LAN or VPN

To serve the same graph to other computers on a LAN or VPN, use the helper and
the printed host IP from the serving machine:

```bash
scripts/serve-lan.sh --use-postgres
```

On Windows:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres
```

Then open `http://<host-ip>:8000/app` from the other computer, or set
`LAB_TRACKER_BASE_URL=http://<host-ip>:8000` for MCP clients. If remote
clients time out, your OS firewall may need an inbound rule for TCP port 8000.
The LAN helpers refuse to bind `0.0.0.0` when authentication is disabled unless
you pass the explicit insecure-demo override documented in the LAN guide.

LAN serving, firewall rules, and phone capture are documented in full in
[`lan-shared-graph.md`](lan-shared-graph.md) and
[`phone-capture-quickstart.md`](phone-capture-quickstart.md).

## Frontend build

The frontend bundle is committed to the repo and served from
`src/lab_tracker/frontend/app.js`. **You only need to rebuild it when you change
the frontend source** in `src/lab_tracker/frontend_src`:

```bash
npm install
npm run lint:frontend
npm run build
```

The committed frontend bundle ships without a source map by default.

## Database migrations

Alembic owns the schema. To apply the latest migrations:

```bash
uv run alembic upgrade head
```

`lab-tracker serve` and the LAN helpers run this for you. The Alembic head and
branch policy lives in the project `CLAUDE.md`.

For local SQLite databases, create an explicit backup before risky changes:

```bash
lab-tracker backup --to /path/to/off-machine-or-synced-backups
```

Restore only after stopping Lab Tracker:

```bash
lab-tracker restore /path/to/backup.sqlite3 --force
```

## First-admin setup

A fresh auth-enabled instance shows a first-admin setup screen while no users
exist. After the first admin exists, use the `Users` screen to grant
viewer/editor/admin roles, create email invitation links, and reset passwords,
and use each project's `Project Members` panel to grant project
viewer/contributor/owner access.

### Non-Docker

Set the token before starting the app:

```bash
export LAB_TRACKER_AUTH_ENABLED=true
export LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN="<one-time-admin-token>"
lab-tracker serve
```

Open `http://127.0.0.1:8000/app` and use `Create First Admin`. The setup screen
loads the bootstrap token while the instance has no users.

### Docker, managed, and disclosure modes

For the Docker first-run flow (where the container generates and persists the
token), the `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE` modes, and ongoing
role/invite management, see
[`self-hosted-operations.md`](self-hosted-operations.md) and
[`one-click-cloud-deploy.md`](one-click-cloud-deploy.md).

The auth-enabled behavior (`LAB_TRACKER_AUTH_ENABLED` defaults and the rule that
local dev starts with auth disabled while non-local is always enabled) is
documented in [`configuration.md`](configuration.md).

## Validation

### Backend

```bash
uv run pytest -q
uv run ruff check .
uv run mypy
```

### Frontend

Run the frontend checks only when you change `src/lab_tracker/frontend_src` or
the committed bundle in `src/lab_tracker/frontend`:

```bash
npm run test:frontend
npm run lint:frontend
npm run build
```

## Related docs

- [Configuration reference (env vars, AI/multimodal, auth)](configuration.md)
- [Supported v1 surface (authoritative)](retained-v1-surface.md)
- [Deployment options overview](deployment-options.md)
- [One-click cloud deploy (Render)](one-click-cloud-deploy.md)
- [Self-hosted operations (backup/restore/upgrade, first admin)](self-hosted-operations.md)
- [Serve the shared graph on a LAN/VPN](lan-shared-graph.md)
- [Phone capture quickstart](phone-capture-quickstart.md)
- [Windows fresh-clone setup](windows-fresh-clone.md)
