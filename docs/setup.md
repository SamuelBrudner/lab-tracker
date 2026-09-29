# Install and Run Lab Tracker

This is the full local setup guide: prerequisites, install, running the API,
the frontend build, database migrations, first-admin setup, and validation. It
is for the lab member or IT contact who installs software. Bench scientists who
just need to *use* an instance their lab already runs do not need this page —
open the link your admin gave you and sign in.

The supported runtime surface is defined in
[`retained-v1-surface.md`](retained-v1-surface.md); if it and this guide
disagree, the retained-surface document wins.

Once the server runs, the [capture guide](capture-guide.md) shows how to wire
each way your lab works (phone and bench, notebooks and scripts, runs and
pipelines, watch folders, coding agents, and server channels) into it.

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
uv sync --frozen --extra test --extra lint
source .venv/bin/activate
```

`uv sync --frozen` creates `.venv` and installs the exact dependency versions
recorded in `uv.lock`, so a new upstream release cannot change your install.
Install `uv` first if needed (for example: `brew install uv` or `pipx install uv`).

### With pip and venv (fallback)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test,lint]"
```

pip resolves the version ranges in `pyproject.toml` rather than the tested
versions in `uv.lock`. Commands below use `uv run`. If you used pip/venv
instead, drop the `uv run` prefix.

The `[test,lint]` extras pull in the backend test and lint tooling. To capture
Matplotlib figures with the Python client (`lab_tracker_client.savefig`,
`capture_figures`), also install the `figure` extra, which adds `matplotlib`
and `pillow`:

```bash
uv sync --frozen --extra test --extra lint --extra figure
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
It refuses to write into a non-local (`LAB_TRACKER_ENVIRONMENT` other than
`local`) or auth-enabled database unless you pass `--allow-non-local`.
This is the same seeded data behind the read-only public demo.

Add `--with-review` to also stage a golden day of fourteen captures (bench and
imaging notes, a figure, a git commit, a meeting note, a bare identifier) and
one READY batch draft over them, so the review page has something to review:

```bash
lab-tracker seed-demo --with-review
```

The batch is produced by a scripted client through the ordinary batch drafting
service; it makes no provider call and needs no `LAB_TRACKER_GRAPH_DRAFT_*`
settings. It is idempotent per demo project: re-running it (or running it after
a plain `seed-demo`) reuses the existing golden-day batch instead of adding
another. The printed summary reports `staged_note_count` and
`review_change_set_id`.

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
`.cursor/mcp.json`, `.gemini/settings.json`, the `scripts/lt.py` shim, and
`AGENTS.lt.md` — are rewritten to the current canonical text. A file whose
content differs is first preserved next to itself as `*.bak-lt-update`, and
`lt_ids.json` is never touched. `--dry-run` previews the changes; run
`lt doctor` afterwards to confirm the repo is in sync. Hook entries that
`lt setup agent-hooks --shared` added are carried forward into the refreshed
`.claude/settings.json` rather than dropped; the personal
`.claude/settings.local.json` is never touched.

`lt update --skills-only` (equivalently `lab-tracker update --skills-only`) is
the machine-wide counterpart for the setup skill. It installs or refreshes only
the `lab-tracker-setup` skill in the Claude and Codex skill homes
(`~/.claude/skills` and `~/.agents/skills`, or the single home named by
`LAB_TRACKER_SKILLS_HOME`) and never touches the current directory, any
repository, or the applied-repos registry, so it runs from anywhere and needs
no repo. A customised skill is preserved next to itself as
`SKILL.md.bak-lt-update`, and `--dry-run` previews. `lt setup status` suggests
it when a skill is missing or stale. Because it never reads a repo, it refuses
`--yes` and `--target`. `lt update --install-skills` still refreshes the skill
in addition to the repo.

### Know when a client install is broken or behind its server

A release is the `[project].version` in `pyproject.toml`, versioned by
[versioning.md](versioning.md) (Semantic Versioning; while on `0.y.z`, MINOR
for features and any incompatibility, PATCH only for backward-compatible
fixes). The server reports it as `app.version` on `GET /health`, next to
`app.source_revision`. The release status is truthful: a client on any older
release is *behind*. One rule decides whether that is worth a nag: an update is
*recommended* (`update_recommended`) only when the client's `MAJOR.MINOR` is
older than the server's. Every notice below keys on that rule, so a PATCH-only
gap is reported as information and never suggested. Revision drift within one
release is reported (`same_revision`) but never suggested either, since most
commits are not consumer-relevant.

- `lt doctor` (and `lt doctor --all`, once per sweep) imports the MCP server
  module in a child interpreter, the same Python `lt` runs from, and reports
  `lt_mcp.importable`, with the error, a bounded traceback tail, and next step
  when it fails. The child has a 15-second limit, and a timeout, a non-zero
  exit (a `sys.exit` or a crash while importing), and an import error each
  report `importable: false`, so a hung or crashing import can neither hang
  nor end `lt`. A failure exits `1` like drift; `--fail-silent` keeps prompt
  hooks quiet. There is no network I/O; `lt setup verify-mcp` remains the
  deeper connectivity check.
- `lt setup status` reports the same `lt_mcp` check plus a `client` release
  comparison built from its existing `/health` probe (`status`,
  `client_behind_server`, `update_recommended`), and suggests the update only
  when one is recommended, so the SessionStart hook's `--brief` line names it.
  The probe has 2-second connect and read timeouts and a 4-second limit on
  receiving the whole response, so a server that sends its headers and then
  trickles the body cannot hold the hook open.
- `lt-mcp` over stdio makes one unauthenticated `GET /health` at startup with
  the same 2-second timeouts and 4-second response limit. It is advisory only:
  any failure, including one while building the HTTP client, is written to
  stderr and leaves the session unchanged.
  When an update is recommended, the MCP `instructions` start with an
  `UPDATE AVAILABLE` notice and every tool result carries the same notice in
  `_lab_tracker_update_notice`. A hosted endpoint skips the check; it ships
  with its server.
- Captures always record `capture_client_version` (`0.0.0+unknown` when the
  client cannot read its own release) and, when known, `capture_client_revision`
  next to the host identity. The coverage read
  (`GET /projects/{project_id}/coverage`) judges each capture source on its
  own: it reports the release the source's newest capture was made with, its
  `release_status` against the server's, and `update_recommended`, and writes
  an `update_notice` on every source for which an update is recommended and
  that captured within the report's `quiet_window_days` (30 days, the same
  window after which a silent source counts as retired). A source with an
  install id but no recorded release predates release reporting; while the
  server's release is known it is reported as behind, with a notice saying
  so. The Daily review page lists each notice, for example "lab-tracker on
  the machine watching `fly_walking_data` (rig-7) is behind this server", and
  the home page's Capture health card marks every behind source "client
  behind". Only a watch source is named by the folder it watches. A capture
  queued offline carries the release that queued it, so draining an old
  queue after an update can show a notice until that source's next live
  capture.

One install id covers every Python environment on a machine, and the notice's
fix depends on which environment made the capture:

- **Tool environment** (`lt watch`, `lt-hpc`, the repo and git hooks,
  `lt import-folder`, and anything else launched from the `uv tool` install):
  install the server's release with the Agents page's install command
  (`uv tool install --force "lab-tracker @ git+https://github.com/SamuelBrudner/lab-tracker.git@<revision>"`),
  then run `lt update` in each consumer repo and restart the MCP host so it
  launches the new `lt-mcp`.
- **Analysis repo** (in-script captures such as `savefig` from
  `lab_tracker_client`, adapter `lab-tracker-client-figure`, or captures that
  carry `run_*` metadata): in that repo, rerun the Setup page's pinned project
  dependency (`uv add "lab-tracker @ git+https://github.com/SamuelBrudner/lab-tracker.git@<revision>"`,
  guided setup step 5). `lt update` refreshes integration files only and does
  not change that pin.

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

Authentication needs a strong signing secret; the built-in placeholder is
rejected at startup whenever auth is enabled. The first line below generates
one into a private file outside the checkout only if that file does not exist
yet, so the same block is safe to rerun on every restart:

```bash
[ -f ~/.lab-tracker-auth-secret ] || (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > ~/.lab-tracker-auth-secret)
export LAB_TRACKER_AUTH_ENABLED=true
export LAB_TRACKER_AUTH_SECRET_KEY="$(cat ~/.lab-tracker-auth-secret)"
export LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN="<one-time-admin-token>"
lab-tracker serve
```

Keep that file private (or move the value into your secrets manager) and keep
using the same value; a new secret signs every user out and invalidates
outstanding invitation links.

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
npm run test:frontend:chaos
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
- [Capture guide (every capture path and its setup)](capture-guide.md)
- [Phone capture quickstart](phone-capture-quickstart.md)
- [Windows fresh-clone setup](windows-fresh-clone.md)

## CLI connection errors

If the API is unreachable, `lt health`, `lt readiness`, and other commands
print `error: <API message>` to stderr and exit with code 1. Successful JSON
output stays on stdout. To include the Python traceback when troubleshooting,
put the global debug option before the command:

```bash
lt --debug health
lt --debug readiness
```

Alternatively, set `LAB_TRACKER_DEBUG=1` in the client environment. Debug mode
does not override an explicit `--fail-silent` hook invocation. Invalid command
arguments retain argparse's exit code 2; unexpected programming errors still
surface normally.

The error names the failing connection stage where it can be observed.
`lt setup status` (`server`) and `lt setup connect --base-url <url> --dry-run`
(`server_diagnostic`) return the same `diagnosis`, `detail`, and `next_step`
fields; see
[Diagnose an unavailable connection](agent-setup.md#diagnose-an-unavailable-connection).
If the server is published through a public Tailscale Funnel, the host-side
checklist is
[Publishing Through Tailscale Funnel](self-hosted-operations.md#publishing-through-tailscale-funnel).
