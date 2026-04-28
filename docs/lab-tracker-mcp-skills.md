# Lab Tracker MCP, Skills, and Dolt Mirror

Lab Tracker uses the API as the single write path for browser users, MCP clients,
scripts, and future workers. For multi-client work, run the app against Postgres
and point assistant MCP clients at the running API.

## API-Backed MCP Server

Run the MCP server with:

```bash
python -m lab_tracker.mcp_server
```

Required environment for read/write tools:

```bash
LAB_TRACKER_MCP_BASE_URL=http://127.0.0.1:8000
LAB_TRACKER_MCP_USERNAME=<service-account-username>
LAB_TRACKER_MCP_PASSWORD=<service-account-password>
```

The server does not store bearer tokens. It logs in with the configured service
account and retries once after a 401.

Available tools:

- `lab_tracker_health`
- `lab_tracker_readiness`
- `lab_tracker_list_projects`
- `lab_tracker_list_questions`
- `lab_tracker_list_notes`
- `lab_tracker_search`
- `lab_tracker_create_project`
- `lab_tracker_create_question`
- `lab_tracker_create_note`

## Postgres Runtime

For multiple live clients, prefer Postgres:

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

SQLite remains the local fallback for simple single-client development.

## Dolt Mirror

Dolt is an export-only versioned mirror in v1. The live API database remains the
source of truth.

```bash
python -m lab_tracker.dolt_mirror export --message "Lab Tracker snapshot"
```

Defaults:

- Mirror path: `.lab-tracker-dolt/`
- Dolt binary: `dolt`
- Override binary with `LAB_TRACKER_DOLT_BIN`
- Override mirror path with `LAB_TRACKER_DOLT_MIRROR_PATH`

The exporter mirrors retained Lab Tracker tables and excludes `users`.

## Skill

The skill source lives at:

```text
skills/lab-tracker/SKILL.md
```

On this machine it should be installed into both assistant homes:

```text
C:\Users\snb6\.codex\skills\lab-tracker
C:\Users\snb6\.claude\skills\lab-tracker
```

Restart Codex or Claude after changing MCP or skill config so the new server and
skill are loaded.
