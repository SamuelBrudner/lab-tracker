# Serving the Shared Graph Across Computers

Use this workflow when one Lab Tracker instance should serve the same live
backend graph to browsers, Codex, Claude, scripts, or MCP clients on other
computers, including computers on different networks.

## Recommended Shape

- Run one Lab Tracker API server as the shared write path.
- Use Postgres as the live source of truth for multi-client work.
- Reach the server through a trusted LAN, VPN, or overlay network such as
  Tailscale before considering public exposure.
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
set real secrets:

```powershell
$env:LAB_TRACKER_ENVIRONMENT = "production"
$env:LAB_TRACKER_AUTH_ENABLED = "true"
$env:LAB_TRACKER_AUTH_SECRET_KEY = "<long-random-secret>"
$env:LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN = "<one-time-admin-bootstrap-token>"
```

The bootstrap token is only needed to create the first admin account. Run this
from the host machine after the app starts:

```powershell
$body = @{
    username = "sam"
    password = "<strong-password>"
    role = "admin"
    bootstrap_token = $env:LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:8000/auth/register" `
    -ContentType "application/json" `
    -Body $body
```

After that, admin users can provision editor accounts. Public in-app
registration creates viewer accounts.

## Start the Shared Server

On Windows, use the helper:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres
```

The helper runs migrations, prints reachable URLs, and starts:

```powershell
.venv\Scripts\python.exe -m uvicorn lab_tracker.asgi:app --host 0.0.0.0 --port 8000
```

If you do not want the helper to set the local Postgres URL, omit
`-UsePostgres` and rely on `.env` or the current shell environment.

If PowerShell blocks local script execution, run the same helper through a
one-shot execution-policy bypass:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\serve-lan.ps1 -UsePostgres
```

If you are using a tunnel or reverse proxy with a known public URL, include it
so the helper prints the browser and MCP base URL shape:

```powershell
.\scripts\serve-lan.ps1 -UsePostgres -ExternalBaseUrl "https://lab-tracker.example.net"
```

## Same LAN or VPN

On another computer connected to the same LAN or VPN:

1. Open `http://<host-ip>:8000/health`.
2. Confirm the response includes `"status": "ok"`.
3. Open `http://<host-ip>:8000/app`.
4. Log in if authentication is enabled.

## Different Networks

The recommended cross-network path is a private overlay network such as
Tailscale:

1. Install and sign in to the same tailnet on the serving machine and client
   computers.
2. Run `.\scripts\serve-lan.ps1 -UsePostgres` on the serving machine.
3. Use the printed `Tailscale tailnet URLs`, or run `tailscale ip -4` on the
   serving machine and open `http://<tailscale-ip>:8000/app`.

This keeps the Lab Tracker port private to machines in the tailnet while still
working across home, campus, and cloud networks.

Temporary public tunnels such as Cloudflare Tunnel or ngrok can also forward a
remote HTTPS URL to `http://127.0.0.1:8000`. Keep Lab Tracker auth enabled, use
the tunnel provider's access controls when available, and avoid leaving a
temporary tunnel running unattended.

Avoid forwarding TCP port 8000 directly from a public router to the app. If you
must expose it publicly, put it behind a real HTTPS reverse proxy and keep auth
enabled.

## Windows Firewall

Opening inbound ports requires administrator privileges. If another computer
times out when opening the printed LAN, VPN, or tailnet URL, run this on the
host machine in PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Lab Tracker TCP 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Domain,Private
```

## MCP Clients

For MCP clients on other computers, set the base URL to the reachable host,
without `/app`:

```powershell
$env:LAB_TRACKER_MCP_BASE_URL = "http://<host-or-tailnet-ip>:8000"
```

When authentication is enabled, also set service-account credentials in that
client environment:

```powershell
$env:LAB_TRACKER_MCP_USERNAME = "<service-account-username>"
$env:LAB_TRACKER_MCP_PASSWORD = "<service-account-password>"
```

For a public HTTPS tunnel, use that tunnel origin instead:

```powershell
$env:LAB_TRACKER_MCP_BASE_URL = "https://lab-tracker.example.net"
```
