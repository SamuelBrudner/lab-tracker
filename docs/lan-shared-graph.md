# Serving the Shared Graph on a LAN

Use this workflow when one Lab Tracker instance should serve the same live
knowledge graph to browsers, Codex, Claude, scripts, or MCP clients on other
computers.

## Recommended Shape

- Run one Lab Tracker API server as the shared write path.
- Use Postgres as the live source of truth for multi-client work.
- Point browsers and MCP clients on other machines at that API URL.
- Keep authentication enabled when serving beyond one trusted local machine.

SQLite remains useful for single-client local testing, but do not use a shared
SQLite file as the multi-computer runtime.

## Start Postgres

From the repo root:

```powershell
docker compose up postgres
```

In the shell that will run the app:

```powershell
$env:LAB_TRACKER_DATABASE_URL = "postgresql+psycopg://lab_tracker:lab_tracker@127.0.0.1:5432/lab_tracker"
```

If this instance is reachable by other people or machines, keep auth enabled and
set a real signing secret:

```powershell
$env:LAB_TRACKER_ENVIRONMENT = "production"
$env:LAB_TRACKER_AUTH_ENABLED = "true"
$env:LAB_TRACKER_AUTH_SECRET_KEY = "<long-random-secret>"
```

## Start the LAN Server

On macOS or Linux, use the helper:

```bash
scripts/serve-lan.sh --use-postgres
```

It runs migrations, prints `http://<lan-ip>:8000/app` and
`http://<lan-ip>:8000/app/capture`, and prints a terminal QR code for the phone
capture URL when `segno` is installed.

On Windows, use the helper:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres
```

The helper runs migrations, prints URLs such as `http://<lan-ip>:8000/app`, and
starts:

```powershell
.venv\Scripts\python.exe -m uvicorn lab_tracker.asgi:app --host 0.0.0.0 --port 8000
```

If you do not want a helper to set the local Postgres URL, omit
`--use-postgres` or `-UsePostgres` and rely on `.env` or the current shell
environment.

The LAN helpers refuse to bind `0.0.0.0` while effective Lab Tracker settings
leave authentication disabled. For a trusted temporary demo only, override this
with `scripts/serve-lan.sh --allow-insecure-auth-disabled` or
`.\scripts\serve-lan.ps1 -AllowInsecureAuthDisabled`.

## Windows Firewall

Opening inbound ports requires administrator privileges. If another computer
times out when opening the printed LAN URL, run this on the host machine in
PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Lab Tracker TCP 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Domain,Private
```

## Other Computers

On another computer connected to the same LAN or VPN:

1. Open `http://<lan-ip>:8000/health`.
2. Confirm the response includes `"status": "ok"`.
3. Open `http://<lan-ip>:8000/app`.
4. Log in if authentication is enabled.

For MCP clients on other computers, set:

```powershell
$env:LAB_TRACKER_MCP_BASE_URL = "http://<lan-ip>:8000"
```

When authentication is enabled, also set `LAB_TRACKER_MCP_USERNAME` and
`LAB_TRACKER_MCP_PASSWORD` in that client environment.

For phone pairing and capture details, see
[`docs/phone-capture-quickstart.md`](phone-capture-quickstart.md).
