# Configuration reference

This is the configuration reference for Lab Tracker: the `LAB_TRACKER_*`
environment variables read by the application, the MCP service-client and
export-only Dolt-mirror variables read outside the FastAPI app, the multimodal
graph-draft-review configuration and behavior, and the local evidence-inbox
import (`lt import-folder`) configuration.

The supported runtime surface is defined in
[`retained-v1-surface.md`](retained-v1-surface.md); if it and this document
disagree, the retained-surface document defines the supported runtime.

## Environment variables

Environment variables are loaded with the `LAB_TRACKER_` prefix. The defaults are
suitable for local development.

### Application

- `LAB_TRACKER_APP_NAME`: FastAPI title (default: `lab-tracker`)
- `LAB_TRACKER_ENVIRONMENT`: environment label (default: `local`)
- `LAB_TRACKER_LOG_LEVEL`: logging level (default: `INFO`)

### Database and storage

- `LAB_TRACKER_DATABASE_URL`: SQLAlchemy database URL (default: `sqlite+pysqlite:///./lab_tracker.db`)
- `LAB_TRACKER_BACKUP_PATH`: SQLite snapshot directory used by `lab-tracker
  serve` and `lab-tracker backup` (default: `~/.lab-tracker/backups`)
- `LAB_TRACKER_BACKUP_KEEP`: number of newest SQLite snapshots to keep when a
  backup runs (default: `10`)
- `LAB_TRACKER_FILE_STORAGE_PATH`: file storage directory (default: `./file_storage`)
- `LAB_TRACKER_NOTE_STORAGE_PATH`: note storage directory (default: `./note_storage`)

SQLite is the default single-client local fallback. For multi-client runtimes,
point `LAB_TRACKER_DATABASE_URL` at Postgres and keep writes behind the Lab
Tracker API. `lab-tracker serve` creates a SQLite snapshot before applying
migrations when the configured database is file-backed SQLite. For a backup on
another disk or synced destination, run `lab-tracker backup --to <path>` and copy
that destination through your normal off-machine backup process.

### Authentication and invitations

- `LAB_TRACKER_AUTH_SECRET_KEY`: auth signing secret (default allowed only in `local`)
- `LAB_TRACKER_AUTH_TOKEN_TTL_MINUTES`: access token lifetime (default: `720`)
- `LAB_TRACKER_AUTH_INVITE_TTL_HOURS`: signed invitation link lifetime
  (default: `168`)
- `LAB_TRACKER_AUTH_RATE_LIMIT_ATTEMPTS`: failed login attempts, or register
  attempts from one caller, allowed per window (default: `10`)
- `LAB_TRACKER_AUTH_RATE_LIMIT_WINDOW_SECONDS`: rate-limit window in seconds
  (default: `60`)
- `LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED`: allow public
  self-registration for viewer accounts (default: `true`). Set to `false` to
  require invites or an admin bearer token for new users.
- `LAB_TRACKER_AUTH_ENABLED`: enable login and role enforcement (default: `false`
  in `local`, `true` otherwise; non-local environments cannot disable auth)
- `LAB_TRACKER_PUBLIC_BASE_URL`: public URL used in email invitation links
- `LAB_TRACKER_CANONICAL_BASE_URL`: permanent base URL used to mint `@id`
  identifiers in PROV-O/JSON-LD provenance documents and `lt export` sidecars
  (default: empty — identifiers are rooted at whatever host served the
  request). Set this once, before the first archived export, to the URL your
  lab commits to long-term; identifiers then stay byte-identical no matter
  which host or port serves the request. See
  [provenance-export.md](provenance-export.md) for the identifier policy.
- `LAB_TRACKER_USAGE_EVENTS`: enable local usage telemetry writes (default:
  `false` in `local`, `true` otherwise)

### Uploads and managed files

- `LAB_TRACKER_MAX_UPLOAD_BYTES`: maximum raw upload size for note files,
  dataset files, and visualization assets (default: `104857600`, 100 MiB).
  Uploads that exceed the limit are rejected and partial local files are
  cleaned up.

### Outbound HTTP policy

HTTP(S) external-artifact resolution and HTTP data-store health share one
runtime destination policy and one pinned HTTP client. The policy is independent
of an artifact pointer's content hash and response-size limits. A public
destination is eligible only when every address returned for its hostname is
globally routable. Malformed URLs, URLs containing user information,
localhost/local/single-label names without an exact internal exception, unsafe
literal or resolved addresses, and DNS answers that mix public and non-public
addresses are denied before an HTTP request is sent. Link-local
metadata-service, unspecified, multicast, reserved, and IPv6 transition
addresses cannot be enabled even by an internal exception. The connection uses
one of the already-vetted numeric addresses, so a second DNS answer cannot
change its destination. Proxy environment variables are ignored. Redirects
have a finite limit, and every redirect target goes through the same
authorization and address-pinning process before the next request. One total
wall-clock deadline covers DNS, connect and TLS setup, response headers, and
every redirect hop. Artifact resolution additionally includes body
verification and hashing in that same deadline.
DNS lookups use the host's configured DNS servers and search domains through
dnspython so they can be cancelled at the deadline. Names available only
through platform-specific NSS, mDNS, or local-hosts integrations may therefore
need a normal DNS record.

Private or otherwise non-public destinations are deny-by-default. Operators can
opt in a destination only with both of these settings:

- `LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES`: comma-separated exact
  HTTP(S) authorities. Each entry is normalized to its scheme, hostname, and
  effective port; for example, `https://files.lab.example` and
  `https://files.lab.example:443` identify the same authority. Entries cannot
  contain user information, paths, queries, fragments, wildcards, or suffix
  patterns.
- `LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS`: comma-separated IPv4 or IPv6
  CIDRs containing the approved destination addresses, for example
  `10.42.0.0/16,fd42:1234::/48`.

The two settings are conjunctive: the normalized request authority must be an
exact configured authority **and** every DNS answer (or the literal IP) must
fall within a configured CIDR. An authority without a network, a network
without an authority, or one unapproved address in a multi-address answer is
denied. Invalid authority or CIDR configuration fails application startup
rather than weakening the policy.

Request duration is controlled separately:

- `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS`: total wall-clock budget for one
  HTTP artifact resolution or HTTP store-health probe, including DNS, connect
  and TLS setup, response headers, and redirects. Artifact resolution also
  includes body verification and hashing (default: `30`). The value must be
  finite, greater than zero, and no greater than `86400` seconds (one day);
  invalid values fail application startup.

This opt-in changes only whether the host may make the outbound connection. It
does not bypass resolve-by-entity or store-health authorization and opaque
not-found behavior, does not weaken full-content hash verification, and does
not increase the configured fetch or returned-content bounds. See
[`external-artifact-resolution-design.md`](external-artifact-resolution-design.md)
for the complete resolution contract.

### External-artifact resolution admission

`POST /external-artifacts/resolve` is admission-controlled independently of
the resolver's HTTP and subprocess deadlines. Authentication completes first;
then the service either obtains a slot immediately or returns one fixed generic
`429` response with `Retry-After`. The response intentionally does not reveal
whether the global or caller-specific limit was full, or anything about the
requested project or entity. A rejected request does not create the ordinary
request database session or begin artifact resolution.

- `LAB_TRACKER_ARTIFACT_RESOLUTION_GLOBAL_IN_FLIGHT_LIMIT`: maximum concurrent
  resolutions in one application process (default: `8`). It must be a positive
  integer no greater than `32`.
- `LAB_TRACKER_ARTIFACT_RESOLUTION_PER_ACTOR_IN_FLIGHT_LIMIT`: maximum
  concurrent resolutions for one authenticated `actor.user_id` in that process
  (default: `2`). It must be a positive integer no greater than the configured
  global limit.

### Data-store health control plane

`GET /data-stores/{store_id}/health` has its own no-wait admission policy.
Authentication completes first. A matching request that cannot obtain both its
process-wide and per-user slot returns one fixed generic `429` response with
`Retry-After` before the ordinary request-scoped database session is allocated.
Authentication services may use their own authoritative database scope before
this point.

An admitted request authorizes and loads the store through the same opaque
project/group boundary as other targeted reads. It then copies only the exact
probe inputs into an immutable value and closes the request database scope
before cache lookup or host I/O. Authorization runs on every request, including
cache hits. Hidden and absent stores therefore remain indistinguishable and
never reach the cache or probe.

HTTP stores use `endpoint` whenever it is present and use `root` only when
`endpoint` is absent. A present blank, malformed, or structurally invalid
endpoint therefore fails closed and never falls back to `root`; the selected
initial URL must also pass the hardened registered-base structural grammar
before host I/O. The health probe sends `HEAD` through the same
outbound policy, pinned client, and total deadline as HTTP artifact resolution.
Statuses `301`, `302`, `303`, `307`, and `308` are followed manually while
preserving `HEAD`; every hop is reauthorized and repinned, safe cross-origin
redirects may proceed, and an HTTPS-to-HTTP downgrade is denied. A terminal
`2xx`, `403`, or `405` response counts as reachable. Policy denials, redirect
loops or limit exhaustion, transport/deadline failures, and other terminal
statuses all return the same static redacted health detail.

- `LAB_TRACKER_STORE_HEALTH_GLOBAL_IN_FLIGHT_LIMIT`: maximum admitted health
  requests in one application process (default: `4`, maximum: `16`).
- `LAB_TRACKER_STORE_HEALTH_PER_ACTOR_IN_FLIGHT_LIMIT`: maximum admitted health
  requests for one authenticated `actor.user_id` in that process (default:
  `1`). Browser, paired-device, and LPAT credentials for one user share this
  capacity.
- `LAB_TRACKER_STORE_HEALTH_CACHE_MAX_ENTRIES`: hard LRU bound for completed
  exact-store health results in one process (default: `256`, maximum: `4096`).
- `LAB_TRACKER_STORE_HEALTH_CACHE_TTL_SECONDS`: monotonic lifetime of a
  completed health result, measured from probe completion (default: `10`,
  maximum: `300`).
- `LAB_TRACKER_STORE_HEALTH_SINGLEFLIGHT_WAIT_SECONDS`: maximum time an
  admitted same-store follower waits for the current probe (default: `10`,
  maximum: `60`). A timeout does not cancel or replace the leader and is not
  cached.

The artifact-resolution and store-health global limits must add up to no more
than `32`, below the standard AnyIO shared worker capacity of 40. This combined
ceiling leaves capacity for authentication, cleanup, and ordinary requests even
when both host-I/O surfaces are saturated.

All admission limits and cache state are process-local, not distributed: each
Uvicorn worker or replica owns independent counters and entries. The supported
deployment therefore uses one Uvicorn worker per service process. Do not treat
these values as cluster-wide quotas; distributed admission is a separate
requirement.

### External rclone and Git artifact resolution

Rclone and Git adapters execute optional host binaries under a separate process
budget. The configured budget is one monotonic deadline for the entire logical
operation: rclone metadata lookup, transfer, and verification share one
deadline, as do Git fetch, object inspection, transfer, and verification. A
store-health probe receives a fresh deadline; Git's URL preflight and HEAD query
share it. Progress or moving between subprocesses does not reset it.

- `LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS`: execution and verification
  budget for one rclone or Git artifact resolution or store-health probe
  (default: `30`). The value must be finite, greater than zero, and no greater
  than `86400` seconds (one day); invalid values fail application startup. This
  setting is independent of `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS`.
- `LAB_TRACKER_RCLONE_ALLOWED_REMOTES`: strict comma-separated exact remote
  names for server-side rclone resolution and rclone store-health probes. The
  unset or empty value denies every remote. Entries are not
  whitespace-trimmed; an empty, malformed, NFKC-delimiter-unsafe, or exact
  duplicate entry fails startup without echoing the configured value. Names
  follow rclone's letters/numbers plus `_-.+@ ` grammar, but cannot begin with
  `-` or space, end with space, contain a colon or separator, or be a
  single-letter Windows drive alias.
- `LAB_TRACKER_GIT_ALLOWED_REMOTES`: strict comma-separated structural grants
  for server-side Git resolution and Git store-health probes. The unset or empty
  value denies every Git remote. Entries are not whitespace-trimmed; an empty,
  malformed, or semantically duplicate normalized entry fails startup without
  echoing the configured value.

Each Git grant must use one of these forms:

- `https://host[:port][/path]`
- `ssh://[user@]host[:port][/path]`
- `git://host[:port][/path]`
- `[user@]host:path` or `[user@]host:/path` (SCP-relative and SCP-absolute
  syntax are distinct)

There are no wildcard or textual-prefix grants. Hostnames are case-normalized
and strictly IDNA-canonicalized, IP literals and default ports are canonicalized,
and a candidate must match the grant's scheme, canonical host, effective port,
SSH user, and path style exactly. Terminal-dot hostnames are rejected rather
than rewritten. A configured path is a case-sensitive prefix of whole path
segments, so a grant for `/lab` permits `/lab/repository.git` but not
`/laboratory/repository.git`. URL roots are valid grants. Non-root path segments
use conservative ASCII letters, digits, and `._~+@-`; empty, repeated, trailing,
dot, leading-dash, or other segments fail closed. Local and drive paths,
remote-helper forms, unsupported schemes, embedded credentials, query or
fragment components, percent escapes, and malformed paths or authorities are
also rejected. Credentials belong in operator-controlled Git credential helpers
or SSH facilities, never in this setting or a persisted store root.

Both policies are parsed once from `Settings` at startup. One immutable instance
of each policy and one bounded process executor are shared by the resolver
registry and store-health checker; those components do not independently reread
the process environment. Rclone health preserves the registered distinction
between `remote:path`, `remote:/path`, and `remote:/`. A present
`credential_ref` remains authoritative even when blank or invalid and never
falls back to the store name. Its bounded `rclone lsf` intentionally reports a
large/noisy root as unreachable when fixed metadata output limits are exceeded.

Store-health Git commands run from an app-owned empty, non-repository directory,
so an ambient checkout's repository-local Git configuration cannot affect them.
The Git command environment clears inherited repository/object/work-tree
selectors and sets the operation directory's parent as Git's discovery ceiling,
preventing that parent or anything above it from supplying repository-local
configuration.

Authorization occurs before process creation. Git's effective remote is then
preflighted with the same bounded command environment. Apart from its required
terminal line ending, `git ls-remote --get-url` output must be byte-for-byte
equal to the reconstructed canonical remote before a query or fetch proceeds;
merely parsing to an equivalent structure is not enough. HTTP redirects are
disabled both generically and for the approved URL. An operator grant therefore
never implicitly approves a rewritten or redirected URL.

The structural policy is an application boundary, not a network sandbox around
Git. Git's system/global configuration remains available for credential
helpers, HTTP proxy and TLS configuration, and SSH uses the host's agent, keys,
and OpenSSH configuration. OpenSSH `HostName`, `ProxyJump`, and `ProxyCommand`,
and Git/HTTP proxy settings can route an approved logical endpoint through
other machines. Treat all of those facilities as trusted, immutable
operator-controlled configuration; users who can modify them can change where
Git connects or disclose Git credentials. Do not mount user-writable Git,
credential-helper, proxy, or OpenSSH configuration into the service.

Every subprocess receives independent stdout and stderr memory caps. Actual
artifact bytes are streamed and checked against the resolver's existing
`max_fetch_bytes` limit as they arrive; a preflight size is advisory and cannot
permit a growing object to exceed that limit. Timeout, output overflow,
malformed metadata, or failed cleanup produces a generic unresolved or
adapter-specific unreachable result without exposing a remote, path, credential,
exception, or raw stderr. Pipes are closed and an uncooperative process is
terminated, then killed and reaped within a separate fixed cleanup grace. A
failed call can therefore exceed the configured execution deadline only by that
bounded cleanup grace.

The bounded rclone/Git process boundary contains complete descendant trees on
both supported process platforms. POSIX hosts use a dedicated process group.
Windows hosts create an unnamed, non-inheritable Job Object configured with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, start the leader suspended, assign and
verify it in the Job Object, and only then resume its primary thread. If secure
containment cannot be established, the child never executes and resolution
fails closed as `UNRESOLVED`.

The deadline and process-output caps bound one resolution, but they do not bound
Git cache growth or concurrent cache mutation. Git fetch disk and cache
containment remain a separate follow-up.

### Bootstrap (first admin)

- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN`: one-time token for creating the first
  admin on fresh auth-enabled deployments
- `LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN_DISCLOSURE`: `local` (default),
  `first_run`, or `never`; controls whether `/auth/bootstrap-status` can return
  the first-admin token before any users exist. In the default `local` mode the
  setup screen shows the token only when the request originates from a local,
  LAN, or VPN address and hides it on public hosts; use `first_run` to allow
  first-run browser display on public deployments; `never` always hides it. The
  token is never returned after any user exists.

### Graph draft providers and transcription

Pick **one** provider and set both halves: `LAB_TRACKER_GRAPH_DRAFT_PROVIDER`
*and* that provider's API key. OpenAI, Anthropic, and Google are equally
supported — the default is only a default. A missing key is not caught at
startup; it surfaces at the first draft as a `failed` change set whose error
names the variable to set. The step-by-step walkthrough (scheduling, agent
credentials, MCP) is [`agent-setup.md`](agent-setup.md).

- `LAB_TRACKER_GRAPH_DRAFT_PROVIDER`: active drafting provider (default:
  `openai`; accepted values are `openai`, `anthropic`/`claude`, and
  `google`/`gemini`; `agentic`/`agentic-openai` enables the read-only agentic
  batch drafter and must be run through the background worker)
- `LAB_TRACKER_GRAPH_DRAFT_BACKGROUND_ENABLED`: when `true`, run-now and
  run-due enqueue graph-draft batch jobs and the in-process worker executes
  them (default: `false`)
- `LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_ENABLED`: when `true`, the app also starts
  an in-process ticker that enqueues due daily-review batches as `SYSTEM`
  (default: `false`)
- `LAB_TRACKER_GRAPH_DRAFT_WORKER_POLL_SECONDS`: worker idle polling interval
  for pending graph-draft batch jobs (default: `5`)
- `LAB_TRACKER_GRAPH_DRAFT_SCHEDULER_INTERVAL_SECONDS`: scheduler tick interval
  for checking due cadence rows (default: `60`)
- `LAB_TRACKER_OPENAI_API_KEY`: required when the provider is `openai` and for
  OpenAI voice-note transcription
- `LAB_TRACKER_OPENAI_MODEL`: OpenAI model for graph drafts (default:
  `gpt-4o-mini`; set another compatible model to override)
- `LAB_TRACKER_OPENAI_REASONING_EFFORT`: optional Responses API reasoning
  effort for graph drafts (`none`, `low`, `medium`, `high`, `xhigh`, or
  `max`; omitted by default)
- `LAB_TRACKER_OPENAI_REASONING_MODE`: optional Responses API reasoning mode
  for graph drafts (`standard` or `pro`; omitted by default). For a
  quality-first GPT-5.6 Sol deployment, use model `gpt-5.6-sol`, effort
  `max`, and mode `pro`. Codex Ultra is a separate agent-orchestration mode,
  not an API reasoning value.
- `LAB_TRACKER_OPENAI_TRANSCRIPTION_MODEL`: OpenAI model for voice-note
  transcription (default: `gpt-4o-mini-transcribe`)
- `LAB_TRACKER_OPENAI_BASE_URL`: OpenAI API base URL (default:
  `https://api.openai.com/v1`)
- `LAB_TRACKER_OPENAI_TIMEOUT_SECONDS`: OpenAI graph draft API timeout in
  seconds (default: `60`)
- `LAB_TRACKER_ANTHROPIC_API_KEY`: required when the provider is `anthropic` or
  `claude`
- `LAB_TRACKER_ANTHROPIC_MODEL`: Anthropic model for graph drafts (default:
  `claude-3-5-sonnet-latest`)
- `LAB_TRACKER_ANTHROPIC_BASE_URL`: Anthropic API base URL (default:
  `https://api.anthropic.com/v1`)
- `LAB_TRACKER_ANTHROPIC_TIMEOUT_SECONDS`: Anthropic graph draft API timeout in
  seconds (default: `60`)
- `LAB_TRACKER_GOOGLE_API_KEY`: required when the provider is `google` or
  `gemini`; also required for Google voice-note transcription
- `LAB_TRACKER_GOOGLE_MODEL`: Google Gemini model for graph drafts and
  transcription (default: `gemini-2.5-flash`)
- `LAB_TRACKER_GOOGLE_BASE_URL`: Google Generative Language API base URL
  (default: `https://generativelanguage.googleapis.com/v1beta`)
- `LAB_TRACKER_GOOGLE_TIMEOUT_SECONDS`: Google graph draft API timeout in
  seconds (default: `60`)

### Daily-review email alerts

Email alerts are per-user and opt-in. They are queued only when an assigned
batch review reaches `ready`; generic graph changes, failed drafts, unassigned
batches, and empty proposals do not send mail. The message deliberately omits
the project name, note text, proposal summary, operation count, and all other
research content. Its signed short-lived link remains a pointer, not an
authorization grant: normal sign-in and project access are still required.

- `LAB_TRACKER_REVIEW_EMAIL_ENABLED`: enable delivery processing (default:
  `false`)
- `LAB_TRACKER_REVIEW_EMAIL_TRANSPORT`: `external` for a mailbox-owned worker,
  or `smtp` for the built-in worker (default: `external`)
- `LAB_TRACKER_REVIEW_EMAIL_WORKER_POLL_SECONDS`: built-in SMTP worker idle
  polling interval (default: `10`)
- `LAB_TRACKER_REVIEW_EMAIL_CLAIM_LEASE_SECONDS`: time before a crashed
  delivery worker's lease can be recovered (default: `300`)
- `LAB_TRACKER_REVIEW_EMAIL_MAX_ATTEMPTS`: provider attempts before a delivery
  becomes terminally failed (default: `8`)
- `LAB_TRACKER_REVIEW_EMAIL_LINK_TTL_MINUTES`: signed review-link lifetime
  (default: `1440`)
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_HOST`: SMTP server hostname
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_PORT`: SMTP server port (default: `587`)
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_USERNAME`: optional SMTP login username
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_PASSWORD`: optional SMTP login password;
  configure it together with the username or configure neither
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_FROM_ADDRESS`: required sender for SMTP
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_TLS_MODE`: `none`, `starttls` (default), or
  `implicit`
- `LAB_TRACKER_REVIEW_EMAIL_SMTP_TIMEOUT_SECONDS`: bounded SMTP timeout
  (default: `10`, maximum: `30`)

Enabling alerts requires authentication and an HTTPS
`LAB_TRACKER_PUBLIC_BASE_URL`. Delivery state is durable in
`review_email_outbox`: unique idempotency keys prevent duplicate enqueue,
leases recover after worker crashes, transient failures back off, and
`accepted` means the provider accepted the message—not that it reached an
inbox. See [daily-review-email-alerts.md](daily-review-email-alerts.md).

For Docker deployments using `external`, invoke the bridge inside the primary
app container or through the root Compose file's default-off
`review-email-external` profile. A bare host invocation uses host defaults and
does not target the deployed Postgres database. The optional
`LAB_TRACKER_AUTH_SECRET_KEY_FILE` escape hatch is consumed only by the
one-shot external bridge; it lets the profile read the app's existing runtime
secret from a read-only volume rather than duplicating that secret in Compose.

### MCP service client (`lt-mcp`)

These variables are read by the MCP server process, not the FastAPI app. The
MCP setup guides ([`lab-tracker-mcp-skills.md`](lab-tracker-mcp-skills.md),
[`lab-tracker-copilot.md`](lab-tracker-copilot.md), and
[`lab-tracker-cursor.md`](lab-tracker-cursor.md)) cover them in context.

- `LAB_TRACKER_MCP_BASE_URL`: Lab Tracker API the MCP server reads from (default:
  `http://127.0.0.1:8000`)
- `LAB_TRACKER_MCP_API_KEY` / `LAB_TRACKER_MCP_TOKEN`: bearer token; either name
  works and bypasses `/auth/login`
- `LAB_TRACKER_MCP_USERNAME` / `LAB_TRACKER_MCP_PASSWORD`: login credentials used
  when no token is set and the target instance has auth enabled
- `LAB_TRACKER_MCP_TIMEOUT_SECONDS`: API request timeout (default: `10`)

The hosted read-only MCP endpoint (the optional `mcp` docker-compose service)
adds:

- `LT_MCP_READONLY_TOKEN`: required bearer token for the hosted endpoint
- `LAB_TRACKER_MCP_TRANSPORT`: `stdio` (default) or `streamable-http`
- `LAB_TRACKER_MCP_HOST` / `LAB_TRACKER_MCP_PORT` / `LAB_TRACKER_MCP_PATH`: bind
  host, port, and path for `streamable-http` (defaults: `127.0.0.1`, `8000`,
  `/mcp`)
- `LAB_TRACKER_MCP_HOST_PORT`: host loopback port the compose `mcp` service is
  published on (default: `9000`)

### Export-only Dolt mirror

- `LAB_TRACKER_DOLT_BIN`: Dolt executable (default: `dolt`)
- `LAB_TRACKER_DOLT_MIRROR_PATH`: local mirror directory (default:
  `.lab-tracker-dolt`)

## Authentication behavior

Local development starts with authentication disabled so early testing can use
the app without creating accounts. Set `LAB_TRACKER_AUTH_ENABLED=true` to test
the login and role flow. Non-local environments keep authentication enabled by
default and cannot disable auth.

Public registration creates viewer accounts when
`LAB_TRACKER_AUTH_PUBLIC_VIEWER_REGISTRATION_ENABLED=true`. Viewer accounts can
inspect authorized records; write workflows (note upload, draft creation,
operation edits, and graph commits) require an editor or admin role. A fresh
auth-enabled instance shows first-admin setup when
`LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN` is configured. `/health` remains public for
uptime probes; `/readiness` and `/metrics` require credentials when
authentication is enabled.

## Usage telemetry

Usage telemetry is local-only. When `LAB_TRACKER_USAGE_EVENTS` is enabled, Lab
Tracker writes rows to the local `usage_events` table through the same API
transaction lifecycle used by HTTP, MCP, and CLI requests. The table records
only verb, resource type, resource UUID, actor UUID/role/principal type, surface
(`http`, `mcp`, or `cli`), project UUID, outcome, timing, and result counts.

Usage events never store titles, bodies, descriptions, transcripts, filenames,
search terms, request bodies, or raw URL paths, and they are intentionally not
included in PROV-O/JSON-LD provenance exports. Admins can inspect aggregate
counts at `GET /usage-events/summary`, export raw usage rows as CSV or JSONL at
`GET /usage-events/export`, and run the one-year raw-event rollup/prune at
`POST /usage-events/retention/run`.

The current egress decision is local-only Postgres/SQLite storage. Actor identity
is stored as the raw local user UUID so operators can answer adoption and support
questions inside their own deployment; changing to a salted per-instance
pseudonym or external sink should happen only behind the existing
`record_usage_event` seam.

## Multimodal graph draft review

Multimodal draft generation runs on whichever provider you configured —
OpenAI (the default), Anthropic, or Google — and requires that provider's API
key. To try the local image review loop with the default provider:

```powershell
$env:LAB_TRACKER_OPENAI_API_KEY = "<your OpenAI API key>"
$env:LAB_TRACKER_OPENAI_MODEL = "gpt-4o-mini"
uv run alembic upgrade head
uv run uvicorn lab_tracker.asgi:app --reload
```

Pair a phone from `Devices`, or use the LAN helper's QR code, then open the
phone capture URL. Capture a photo, voice note, photo+voice bundle, or text
note. Select the project and optional question/session/dataset/analysis/claim
targets, add an optional hint, then choose `Upload and draft`. Raw images and
raw audio are stored first as note artifacts in `LAB_TRACKER_NOTE_STORAGE_PATH`;
voice notes receive editable transcripts linked back to the raw audio. The draft
is stored separately as a `GraphChangeSet` linked back to the source note.

### Draft modes

Draft mode defaults to `graph_context`. In that mode, Lab Tracker builds and
stores a compact context packet containing the source note, selected targets,
project, active/staged questions with parent links, recent notes, sessions,
datasets, analyses, claims, visualizations, and unresolved recent image
captures. Context build failures are loud API errors and do not silently fall
back to OCR or image-only interpretation. Image-only drafting is available only
when explicitly requested and records `draft_mode=image_only`.

### Provider, model, and residency

The configured graph-draft provider receives uploaded image bytes when present,
editable transcript text when present, optional user hint, graph context packet,
and strict operation schema. OpenAI and Google clients can transcribe voice
notes; Anthropic drafting does not provide native audio transcription in this
runtime. Configure provider, model, API key, base URL, and timeout with the
provider-specific variables above. Third-party logging, retention, and
residency depend on the selected provider and base URL. For institutional
deployments, point the active provider's base URL at an approved gateway or
model endpoint.

### Auth, validation, and committed records

Authentication and role checks apply to raw images, drafts, draft edits, and
commits. Viewer accounts can inspect authorized records; editor/admin roles are
required for note upload, draft creation, operation edits, and graph commits.
Raw images and draft operations are not committed automatically. Accepted
operations still pass through the normal API validation path, and model output
that references unknown entity IDs or unsupported semantic operations is rejected.

### Review metadata and evaluation

The review screen records enough metadata to compare `graph_context` and
`image_only` behavior: draft mode, model/provider, context snapshot, uncertainty
fields, clarification requests, operation statuses, and commit timing. Suggested
evaluation metrics are accepted/edited/rejected operations, duplicate entity
proposals, reviewer edit burden, time from capture to commit, and uncertainty
quality. Offline queued capture is intentionally deferred in this release.

## Local evidence inbox imports

Use `lt import-folder` to turn files from a local or synced folder into staged
evidence notes. This works for folders synced by Google Drive, Dropbox, OneDrive,
or similar tools without adding a provider-specific OAuth workflow:

```bash
LAB_TRACKER_BASE_URL=http://127.0.0.1:8000 \
LAB_TRACKER_PROJECT_ID=<project-id> \
lt import-folder --project "$LAB_TRACKER_PROJECT_ID" --root /path/to/lab-inbox
```

The adapter records `evidence_source_*` metadata, skips duplicates by source ID
and content hash, and never commits graph changes — imported files land as
staged evidence notes, and human review remains the commit boundary. See
[`evidence-source-metadata.md`](evidence-source-metadata.md).
Symlinked files are skipped during discovery and are not followed outside the
configured inbox root.

The retained v1 runtime keeps note handling manual and uses direct substring
search for query flows. Deferred concepts live in
[`retained-v1-surface.md`](retained-v1-surface.md) rather than the active
product surface.
