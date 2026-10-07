# Lab Tracker development and operations

## First Moves

1. Read `README.md` and `docs/retained-v1-surface.md` for current product scope.
2. Use `bd ready` and `bd show <id>` for tracked repo work.
3. For multi-client work, prefer Postgres through `docker compose up postgres`
   and set `LAB_TRACKER_DATABASE_URL` to
   `postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker`.
4. Run `uv run alembic upgrade head` before using a fresh database.
5. Use `uv run uvicorn lab_tracker.asgi:app --reload` to serve the app at
   `http://127.0.0.1:8000/app`.
6. To serve one graph to other computers on a LAN or VPN, use
   `.\scripts\serve-lan.ps1 -UsePostgres` and see `docs/lan-shared-graph.md`.
7. A shared instance has one canonical `LAB_TRACKER_BASE_URL`; open its browser
   UI at `<LAB_TRACKER_BASE_URL>/app`.

## MCP connection

The local MCP entry point is `lt-mcp`; `python -m lab_tracker.mcp_server`
also works in a source checkout. It calls the running API rather than writing
directly to the database.

```bash
LAB_TRACKER_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_API_KEY=<lpat-personal-access-token>
```

On another machine, use the deployment's reachable HTTPS origin instead of
localhost. `LAB_TRACKER_MCP_API_KEY` holds a Lab Tracker personal access token
(LPAT), minted by the person on **Agents** (`/app/agents`) or through
`POST /auth/tokens`. The old `LAB_TRACKER_MCP_USERNAME` /
`LAB_TRACKER_MCP_PASSWORD` login is deprecated; `lt auth doctor` detects MCP
configs still using it. Auth-disabled local testing can omit the token.

## Reporting Friction and Failures

When using Lab Tracker surfaces a real problem — a command or MCP tool that
errors, a documented workflow that does not behave as described, setup/startup
that fails, a missing or wrong doc, or friction that blocks or noticeably slows
the user — recommend filing an issue on the GitHub repo so the maintainer sees
it. This is the end-user feedback channel and is deliberately separate from
`bd`/beads, which tracks in-repo development work; do not route user-facing
usage problems into beads.

Offer to file it; do not open the issue silently. Filing is outward-facing, so
confirm with the user first, and check for an existing report before opening a
new one:

```bash
gh issue list --repo SamuelBrudner/lab-tracker --search "<keywords>"
gh issue create --repo SamuelBrudner/lab-tracker \
  --title "<concise summary>" \
  --body "<what you tried, exact command or tool call, full error, environment>"
```

If `gh` is unavailable, share the web link
`https://github.com/SamuelBrudner/lab-tracker/issues/new` together with a
ready-to-paste title and body.

Include in every report:
- What the user was trying to do and the exact command or MCP tool call.
- The full error text, or the observed vs. expected behavior.
- Environment: OS, how the app is served (local, LAN, or Tailscale Funnel),
  whether auth is enabled, and the Lab Tracker version if known.
- Which doc or skill step was being followed, if any.

## Dolt Mirror

Dolt is an export-only versioned mirror for snapshots, diffs, branches, and
later remote sync. The live API database remains the source of truth.

```bash
python -m lab_tracker.dolt_mirror export --message "Lab Tracker snapshot"
```

Defaults: `.lab-tracker-dolt/` for the local mirror and `dolt` for the
executable. Use `LAB_TRACKER_DOLT_BIN` or `LAB_TRACKER_DOLT_MIRROR_PATH` to
override them.

## Quality Gates

Backend:

```bash
uv run pytest -q
uv run ruff check .
```

Frontend, when `src/lab_tracker/frontend_src` or the committed bundle changes:

```bash
npm run test:frontend
npm run test:frontend:chaos
npm run lint:frontend
npm run build
```

## Boundaries

The retained-v1 runtime is defined by `docs/retained-v1-surface.md`. Deferred
ideas from `docs/archive/connected-lab-platform-v1.2.md` should not be treated
as active product requirements unless a bead explicitly says to implement them.
