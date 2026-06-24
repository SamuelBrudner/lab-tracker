# GitHub Copilot MCP Support — Design

Status: **DESIGN**; P0a/P0b/P0c/P1 are implemented in this branch.
Audience: repo maintainer.

Scope anchor: `docs/retained-v1-surface.md` wins over README prose. Product
guardrail (non-negotiable): **"AI can suggest; only a person commits."**

## Decisions that scope this design

These were chosen deliberately and constrain everything below:

1. **Target GitHub Copilot in *human-driven IDEs only*** — VS Code, Visual
   Studio, JetBrains/Eclipse/Xcode agent mode. The autonomous **github.com
   coding agent is out of scope**. Consequence: **no public internet exposure is
   required**; a private VPN/tailnet endpoint is sufficient, and we never need
   Tailscale Funnel, `copilot-setup-steps.yml`, or a `COPILOT_MCP_*` bot config.
2. **All remote/hosted surfaces are read-only.** Writes happen only via a
   developer's own local (stdio) MCP process or the app UI. Consequence: there
   are no remote writes to attribute, so the per-request **token-forwarding**
   machinery and its cross-tenant-leakage hazards are **not needed**; a shared
   (or per-user) **read-only** token is enough for the hosted endpoint.
3. **Persisted as this design + tracked beads** before any code.

"Copilot" here is **GitHub Copilot**, the developer-tool family that natively
consumes MCP servers. Microsoft 365 Copilot / Copilot Studio is a different
product (OpenAPI/declarative-agent integration) and is an explicit non-goal.

## Current state (verified against the working tree)

- The MCP server is a **standalone process** that is an **HTTP client** of the
  Lab Tracker API. `mcp_server.py` `main()` reads `LAB_TRACKER_MCP_TRANSPORT`
  and runs stdio by default or streamable HTTP for the hosted endpoint. It is
  **not mounted** in the FastAPI app.
- **39 tools** (25 read / 14 write after reclassifying
  `list_question_refactors`; write tools include `record_evidence_bundle`, whose
  `dry_run` defaults to `True`). P0b registers MCP annotations:
  read tools carry `readOnlyHint=True`; write tools leave `readOnlyHint=False`;
  `refactor_question` and `update_goal` carry `destructiveHint=True`.
- Auth: `LAB_TRACKER_TOKEN` sends a static `lpat_` bearer and skips
  `/auth/login`; compatibility aliases `LAB_TRACKER_MCP_API_KEY`,
  `LAB_TRACKER_MCP_TOKEN`, and `LAB_TRACKER_ACCESS_TOKEN` remain accepted.
  Without a bearer token, username/password are a fallback/local service-account
  login path with one 401 retry (`mcp_api_client.py`). The MCP client sends
  `X-LabTracker-Surface: mcp`.
- Committed client configs now include `.mcp.json` in **Claude/Codex shape**
  (top-level `mcpServers`) plus Copilot-shaped examples `.vscode/mcp.json` and
  `mcp.visualstudio.json` (top-level `servers`). GitHub Copilot IDEs read the
  `servers` schema and will not read `.mcp.json`.
- Auth middleware (`app_parts/middleware.py`) branches on token prefix:
  `ldev_` → device principal; `lpat_` → service principal with capped role and
  `read_only`; otherwise JWT → user. When auth is disabled (the default for
  `LAB_TRACKER_ENVIRONMENT=local`), **every request becomes
  `AuthContext(LOCAL_AUTH_USER_ID, Role.ADMIN)` with no credential**.
- A long-lived, hashed, revocable, per-user token primitive now exists:
  `PersonalAccessTokenService` / `lpat_` tokens (`auth.py`), modeled after
  `DeviceAuthService` / `ldev_`.

## How a developer will use it (two modes)

### Mode 1 — Local per-user stdio (primary)

Each developer runs their **own** MCP process from their IDE; identity is the
process environment, so per-user attribution is automatic and **writes are
allowed** (always human-confirmed in the IDE). This is the simplest, lowest-risk
mode and works today over stdio once a Copilot-shaped config file exists.

- Auth-disabled local dev: no credential needed (config file is enough).
- Auth-enabled: the dev supplies their own credential — an `lpat_` token is the
  nicer, revocable, password-free option; username/password remains a fallback.

### Mode 2 — Shared private read-only hosted endpoint (optional)

One hosted MCP server over the lab's VPN/tailnet, serving **read-only** decision
context to many IDEs. Because it is read-only, inbound auth can be a single
shared read-only `lpat_` (or per-user read-only tokens for nicer revocation), and
no token forwarding is required. Still fronted by TLS + Origin/Host validation.

```
  Mode 1 (stdio, per-user, writes OK)        Mode 2 (hosted, read-only)
  IDE ── spawns ──► lt-mcp (your token) ─► API     IDE ─► TLS proxy ─► lt-mcp (HTTP) ─► API
        identity = your env                              read-only lpat_; reads only
```

## Building blocks

### B1 — Local Copilot IDE onboarding (config + annotations + docs)

The "make Copilot work at all" core. No server-logic risk.

**`.vscode/mcp.json`** (new; VS Code/Visual Studio auto-discover it). Note the
top-level `servers` key and `inputs` for a prompted secret:

```jsonc
{
  "inputs": [
    { "type": "promptString", "id": "lt-token", "description": "Lab Tracker bearer token (lpat_...) - leave blank if auth is disabled", "password": true }
  ],
  "servers": {
    "lab-tracker": {
      "type": "stdio",
      "command": "lt-mcp",
      "env": {
        "LAB_TRACKER_BASE_URL": "http://127.0.0.1:8000",
        "LAB_TRACKER_TOKEN": "${input:lt-token}"
      }
    }
  }
}
```

From-source fallback (no install on PATH), replacing the fragile `PYTHONPATH=src`
launch:

```jsonc
{
  "servers": {
    "lab-tracker": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SamuelBrudner/lab-tracker", "lt-mcp"],
      "env": { "LAB_TRACKER_BASE_URL": "http://127.0.0.1:8000" }
    }
  }
}
```

Also: a Visual Studio-shaped repo-root config (same `servers` schema; flag that
the existing `.mcp.json` uses `mcpServers` and VS will not read it — keep them as
separate documented files); reconcile the committed `.mcp.json` with
`cli.py::_mcp_json()` (they disagree today); a short `instructions=` string on
`FastMCP(...)` carrying the two load-bearing rules (decision-context-first;
"only a person commits") since Copilot has no skills system; and a new
`docs/lab-tracker-copilot.md` user doc + a README "Capture and integrate"
pointer.

**Tool annotations.** Drive annotations from a per-tool table in the existing
`register_*_tools` loops (`from mcp.types import ToolAnnotations`):

- **Read tools** → `readOnlyHint=True`, `openWorldHint=True`, `title=…`. VS Code
  uses `readOnlyHint` to **skip the confirmation dialog**, so reads auto-run.
- **Write tools** → leave `readOnlyHint` false (so Copilot prompts), set
  `destructiveHint` on `refactor_question`/`update_goal`, and front-load
  `record_evidence_bundle`'s docstring with "Defaults to dry-run; pass
  `dry_run=false` to write."
- Reclassify **`list_question_refactors`** as read-only (it is currently in
  `WRITE_TOOLS` but performs no write).

### B2 — `lpat_` personal access token

A new per-user token type, mirroring `DeviceAuthService` method-for-method. Needed
for password-free auth on Mode 1 (auth-enabled) and inbound auth on Mode 2.

Why not reuse: **JWTs** need a resident password and aren't individually
revocable; **`ldev_` device tokens** only permit writes to `/notes` capture
endpoints (`device_principal_can_access`), wrong for a read-everything assistant.

- **Constant:** `LPAT_TOKEN_PREFIX = "lpat_"` in `auth.py`.
- **Principal type:** add `PrincipalType.SERVICE`.
- **DB model** (`db_models.py`, new `personal_access_tokens` table): `id`,
  `user_id` FK, `label`, `token_hash` (unique, indexed; `_hash_token` only —
  never store raw), `role` (capped ≤ issuer's role at issuance), `read_only`
  (bool, default **True**), `created_at`, `last_used_at` (throttled 5 min like
  devices), `revoked_at`, **`expires_at` (REQUIRED, with a max TTL)**.
- **Migration:** additive table. Per CLAUDE.md, run `uv run alembic heads` (must
  be 1), set `down_revision` to it, never renumber; rebase `down_revision` onto
  whatever head exists **at merge time** and run `test_alembic_has_single_head`
  post-merge.
- **Service** `PersonalAccessTokenService`: `issue_token(user, *, label, role,
  read_only, expires_at)` (returns raw secret once; caps role; rejects TTL beyond
  max), `verify_token` (hash lookup; reject revoked/expired; throttled
  `last_used_at`; **no caching of validity** so revocation is immediate),
  `list_tokens`, `revoke_token`.
- **Access policy** `service_principal_can_access(method, path, *, read_only,
  role)` — the **single authorization decision point**: reads allowed everywhere
  except `/auth/*`; writes **denied if `read_only`**, otherwise role-gated.
- **Middleware branch** (new `elif` before the JWT `else`):

  ```python
  elif token.startswith(LPAT_TOKEN_PREFIX):
      principal = await run_in_threadpool(app.state.pat_service.verify_token, token)
      if principal is None:
          raise AuthError("Invalid personal access token.")
      if not service_principal_can_access(request.method, request.url.path,
                                          read_only=principal.read_only, role=principal.role):
          return _device_forbidden_response("Not permitted for this token.")
      user = await run_in_threadpool(app.state.auth_service.get_user_by_id, principal.user_id)
      if user is None:
          raise AuthError("Invalid personal access token.")
      request.state.auth_context = AuthContext(
          user_id=principal.user_id,
          role=principal.role,          # the token's CAPPED role — NOT user.role
          principal_type=PrincipalType.SERVICE,
      )
  ```

  **Trap:** the device branch uses `user.role` (live). The `lpat_` branch MUST
  use `principal.role` (the capped token role), or promoting the user later
  silently un-caps the token. Lock with a test.
- **Endpoints** `/auth/tokens` (issue/list/revoke), mirroring device-management
  routes, with a **dedicated fail-closed throttle on `lpat_` 401/403** (the
  existing `auth_rate_limit_*` only guards `/auth/login`).
- **Belt-and-suspenders:** the path-level `read_only` check is the first gate;
  service-layer `require_role` is the independent second gate. Audit every write
  service for uniform `require_role` before enabling any write.

### B3 — MCP client static API key

In `mcp_api_client.py`: accept the canonical `LAB_TRACKER_TOKEN` plus
compatibility aliases `LAB_TRACKER_MCP_API_KEY`, `LAB_TRACKER_MCP_TOKEN`, and
`LAB_TRACKER_ACCESS_TOKEN` in `MCPSettings`. When present, send it directly as
`Authorization: Bearer <lpat_...>` and **skip `/auth/login`** and the token
cache; a 401 means revoked/expired (fail fast, no replay). MCP-specific aliases
take precedence inside MCP-only runtime config to preserve existing deployments.

**Secret-in-logs hygiene (load-bearing).** `lab_tracker_api_error()` and
`LabTrackerAPIUnavailableError` serialize request lines into tool output returned
to Copilot. **Redact any `lpat_…` / `Bearer …` substring** before it reaches tool
output; strip `Authorization` from proxy/uvicorn logs; rely on the required short
`expires_at` because secrets leak somewhere over their lifetime.

### B4 — HTTP transport (only for Mode 2)

In `mcp_server.py`, make `main()` read env (defaults unchanged):

```python
LAB_TRACKER_MCP_TRANSPORT   # "stdio" (default) | "streamable-http"
LAB_TRACKER_MCP_HOST        # default 127.0.0.1  (loopback — proxy owns TLS)
LAB_TRACKER_MCP_PORT        # default 8000
LAB_TRACKER_MCP_PATH        # default "/mcp"
```

Construct `FastMCP(SERVER_NAME, instructions=…, stateless_http=True,
json_response=True)` for the hosted endpoint, then
`server.run(transport="streamable-http")`. Keep the server **standalone** (its own
process / HTTP client of the API) — do not mount under FastAPI this round (a
mounted sub-app's lifespan isn't run, requiring manual `session_manager.run()`
hoisting). Inbound auth: simplest is a reverse-proxy / ASGI static-header gate
that authenticates the read-only `lpat_`; authorization still happens at the API
(hop 2), not in FastMCP scopes.

## Deployment (Mode 2 only)

Add a loopback-bound `mcp` compose service + a TLS proxy. Because the endpoint is
**private (VPN/tailnet) and read-only**, no Funnel/public exposure is needed; use
tailnet-private `tailscale serve` or a LAN reverse proxy.

```yaml
  mcp:
    build: .
    command: uv run python -m lab_tracker.mcp_server
    environment:
      LAB_TRACKER_MCP_TRANSPORT: streamable-http
      LAB_TRACKER_MCP_HOST: 0.0.0.0              # container bind; host publish below is loopback-only
      LAB_TRACKER_MCP_PORT: "8000"
      LAB_TRACKER_MCP_BASE_URL: http://app:8000  # hop-2 target (must report auth ON)
      LAB_TRACKER_MCP_API_KEY: ${LT_MCP_READONLY_TOKEN}   # a read-only lpat_
    ports:
      - "127.0.0.1:9000:8000"
    depends_on: [app]
```

Caddy terminates TLS, validates Origin/Host, and strips the bearer from logs:

```caddyfile
mcp.lab.internal {
    log { format filter { request>headers>Authorization delete } }
    @badorigin { header Origin *; not header Origin https://github.com; not header Origin https://*.githubcopilot.com }
    respond @badorigin 403
    reverse_proxy 127.0.0.1:9000 { header_up X-Forwarded-Proto {scheme} }
}
```

> The exact Origin values Copilot IDEs send are unverified — treat the allowlist
> as a **placeholder; confirm against live request logs** before enforcing, or
> legitimate traffic will 403. Origin checks defend only the browser threat
> model; for non-browser clients rely on the token + TLS + short TTL + revocation.
> Set **no permissive CORS** on `/mcp`.

**Open-ADMIN footgun (guard required).** When auth is disabled (default for
`environment=local`), the internal `app` service is a fully open ADMIN endpoint.
The hosted `app` MUST set a non-local `LAB_TRACKER_ENVIRONMENT` **and** a real
`LAB_TRACKER_AUTH_SECRET_KEY`. The MCP startup guard refuses to start against a
non-loopback target (`LAB_TRACKER_MCP_BASE_URL` or `LAB_TRACKER_BASE_URL`) that
reports `auth.enabled=false` from `/readiness`.

## Guardrail enforcement

| Surface | Reads | Writes | Enforced by |
|---|---|---|---|
| Local stdio (Mode 1) | yes | yes, role-gated | per-process token Role + `read_only`; human confirms each write in the IDE |
| Hosted (Mode 2) | yes | **no** | read-only `lpat_` (`read_only=true` + VIEWER role → `require_role` denies); write tools surface no `readOnlyHint` so the IDE would prompt even if reached |

"AI suggests, a person commits" holds on both rows via **server-side** controls,
not client config.

## Phased plan

**P0a — Local Copilot IDE onboarding.** `.vscode/mcp.json` + VS config, portable
launch, `instructions=` string, `docs/lab-tracker-copilot.md` + README pointer.
No migration. Makes auth-disabled local + username/password stdio work in Copilot
IDEs immediately. *Lowest risk; do first.*

**P0b — Tool annotations.** `readOnlyHint`/`destructiveHint`/`title` table +
reclassify `list_question_refactors`. No migration. Reads auto-run; writes prompt.

**P0c — `lpat_` primitive + client API key + log hygiene.** Implemented: B2 +
B3 + the open-ADMIN startup guard. **One additive migration.** Unlocks
password-free per-user stdio auth and provides the read-only token for Mode 2.
Test gates:
issue/verify/revoke/role-cap; capped role survives user promotion to ADMIN;
read-only token blocked on every write incl. `record_evidence_bundle(dry_run=false)`;
client skips `/auth/login` with a key; no `lpat_` substring in returned errors;
`test_alembic_has_single_head` green.

**P1 — HTTP transport + private read-only hosted endpoint.** Implemented: B4 +
compose `mcp` service (host-loopback publish) + Caddyfile (Origin allowlist +
log-strip) + tailnet-private deployment docs. No migration. Read-only only. Test
gates: boots streamable-http on loopback; reverse-proxy Origin 403; hosted
`lpat_` cannot write; `/mcp` sets no permissive CORS; startup guard refuses an
auth-off hop-2 target.

**Deferred (not in current scope):** per-request token forwarding (only if remote
writes are ever wanted), OAuth 2.1 PRM, the autonomous coding agent, PyPI publish
+ `server.json` registry entry + "Install in VS Code" badge.

## Recommended path

Build **P0a → P0b → P0c**, then **P1** if a shared hosted read-only endpoint is
wanted. P0a alone makes Copilot users productive locally; P0c lands the
revocable, password-free token model and every server-side control P1 relies on.

## Open decisions (defaults chosen; confirm or override)

1. **`lpat_` TTL & rotation** — default `read_only=true`, role ≤ issuer, max
   `expires_at` currently 90 days; confirm whether hosted tokens should use a
   shorter rotation cadence.
2. **Hosted token shape (Mode 2)** — one shared read-only token (simplest) vs.
   per-user read-only tokens (finer revocation/audit). Recommend per-user if the
   lab is more than a few people.
3. **Is Mode 2 wanted at all now**, or is per-user stdio (Mode 1) enough for the
   foreseeable future? P1 is optional and can be deferred indefinitely.
