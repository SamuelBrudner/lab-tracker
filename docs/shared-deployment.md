# Shared Metadata Deployment

Use this deployment shape when many computers, ChatGPT sessions, or coding agents should see
the same Lab Tracker projects, questions, notes, and sessions.

## Recommended Shape

Run one shared Lab Tracker server backed by one shared Postgres database:

```text
Browsers, ChatGPT, coding agents, local scripts
        -> shared Lab Tracker API/MCP server
        -> shared Postgres metadata database
        -> shared file/note storage volume
```

Avoid many independent local servers writing to SQLite. SQLite is still useful for single-user
local development, but Postgres is the supported shared metadata path.

## Docker Compose

Create a deployment env file from the template:

```bash
cp deploy/shared-metadata.env.example .env
```

Edit `.env` and set strong values for:

- `POSTGRES_PASSWORD`
- `LAB_TRACKER_DATABASE_URL`
- `LAB_TRACKER_AUTH_SECRET_KEY`
- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`
- `LAB_TRACKER_FILE_STORAGE_PATH`
- `LAB_TRACKER_NOTE_STORAGE_PATH`

Start the shared server:

```bash
docker compose up --build -d
```

The app service waits for Postgres, runs `alembic upgrade head`, and then starts the API on
port `8000`. Named Docker volumes persist:

- `postgres_data`: metadata database
- `file_storage`: dataset/file bytes
- `note_storage`: raw note uploads

Check deployment health:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/readiness
```

## First Admin

Use the bootstrap token once to create the first admin account:

```bash
curl -X POST http://127.0.0.1:8000/auth/register \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN" \
  -d '{"username":"admin","password":"replace-me","role":"admin"}'
```

After the first admin exists, create editor/admin accounts through authenticated admin
requests. Public registration creates viewer accounts only.

## Local Installs Pointing At Shared Metadata

For a local non-Docker install that connects to shared Postgres:

```bash
uv pip install -e ".[postgres,mcp]"
export LAB_TRACKER_DATABASE_URL='postgresql+psycopg://user:pass@host:5432/lab_tracker'
export LAB_TRACKER_AUTH_SECRET_KEY='same-signing-secret-used-by-the-shared-server'
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Run migrations from one controlled place. Do not let multiple computers race to migrate the
same shared database during normal use.

## MCP And Agent Access

Prefer connecting ChatGPT and coding agents to the shared server rather than running many MCP
servers with write access against the same database. The coding MCP profile is read-only:

```bash
uv run lab-tracker-coding-mcp
```

For ChatGPT writes, deploy an HTTPS MCP endpoint with authentication before enabling
`LAB_TRACKER_MCP_ENABLE_WRITES=true`. Keep local tunnel-based MCP for development.

## Operational Notes

- Back up `postgres_data`, `file_storage`, and `note_storage` together.
- Keep `LAB_TRACKER_AUTH_SECRET_KEY` stable; rotating it invalidates existing tokens.
- Put the shared API behind HTTPS before use outside a trusted network.
- If using external Postgres, set `LAB_TRACKER_DATABASE_URL` to that service and keep the
  shared file/note storage paths on durable storage mounted by the app server.
