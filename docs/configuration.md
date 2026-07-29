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
- `LAB_TRACKER_SOURCE_REVISION`: full immutable Git revision embedded by a
  reviewed deployment build and reported by health/setup readiness (default:
  `unknown`). Production onboarding withholds matching client-install commands
  unless this is a full 40-character revision.
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

### Scoped store-authority grants

- `LAB_TRACKER_STORE_AUTHORITY_GRANTS_JSON`: the operator-owned, versioned
  project/group store-authority registry. Unset or exactly empty means deny
  all. Any other value must be the exact JSON envelope described below;
  whitespace-only input is invalid. The raw value is treated as sensitive
  configuration and is excluded from `Settings` representations and model
  dumps.

The top-level wire format is an object with exactly these two fields:

```json
{"schema":"lab-tracker/store-authority/v1","grants":[]}
```

The configured value must be compact on one line: raw control characters,
including JSON formatting newlines and tabs, are rejected before decoding.

Each non-rclone grant has exactly five fields:

| Field | Contract |
| --- | --- |
| `grant_id` | Opaque 1–128-character ASCII selector matching `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. It is not authority by itself and is excluded from the semantic fingerprint. |
| `scope` | Exactly `{"project_id":"<canonical UUID>"}` or `{"group_id":"<canonical UUID>"}`. The keys are mutually exclusive and no database lookup occurs at startup. |
| `kind` | One supported `StoreKind`: `local_fs`, `http`, or `git` for their native adapters, or one of the rclone-backed kinds below. `object_table` and `database` are rejected until their adapters and secret models exist. |
| `root` | One kind-specific string boundary, parsed as described below. |
| `capabilities` | A non-empty, duplicate-free list of known `StoreCapability` values. It may narrow but cannot exceed the kind's supported set. |

Scope parsing is syntactic and side-effect free. A canonical UUID that does not
exist in this deployment is accepted into the snapshot, performs no startup
database lookup, and remains inert unless a later authorized operation selects
it.

Rclone-backed grants—`ssh`, `s3`, `gcs`, `azure_blob`, `dropbox`,
`gdrive`, `box`, `onedrive`, and `rclone`—have those five fields plus exactly:

| Field | Contract |
| --- | --- |
| `remote` | The exact effective rclone remote name. This is a handle, never a token, password, or connection string. |
| `credential_mode` | Exactly `name_fallback` or `credential_ref`. `name_fallback` authorizes only a store with no `credential_ref` whose store name equals `remote`; `credential_ref` authorizes only an explicit `credential_ref` equal to `remote`. |

All non-rclone kinds reject `remote` and `credential_mode`. The boundary
grammar is kind-specific:

| Kind | `root` boundary | Supported capability ceiling |
| --- | --- | --- |
| `local_fs` | Strict native absolute lexical path for the current platform; no home/cwd expansion, filesystem lookup, navigation component, or ambiguous POSIX separator | `bytes_by_path`, `byte_range`, `list` |
| `http` | Canonical credential-free HTTP(S) directory prefix: exact origin plus ordered decoded path components | `bytes_by_path`, `byte_range` |
| rclone-backed `ssh`, `dropbox`, `gdrive`, `box`, `onedrive`, `rclone` | Decoded relative or rooted path within `remote`; rootedness and ordered components are significant | `bytes_by_path`, `byte_range`, `list` |
| rclone-backed `s3`, `gcs`, `azure_blob` | Same rclone boundary | `bytes_by_path`, `byte_range`, `list`, `versioned_snapshot` |
| `git` | Full canonical credential-free Git remote: scheme, host, effective port, optional SSH user, path style, and ordered path components | `bytes_by_path`, `byte_range`, `versioned_snapshot` |

For example, this grant uses an explicit rclone credential handle:

```json
{"schema":"lab-tracker/store-authority/v1","grants":[{"grant_id":"project-archive","scope":{"project_id":"123e4567-e89b-42d3-a456-426614174000"},"kind":"s3","root":"/experiments","capabilities":["bytes_by_path","byte_range","list","versioned_snapshot"],"remote":"archive-s3","credential_mode":"credential_ref"}]}
```

Within one exact scope, kind, and effective target family, equivalent or
ancestor/descendant boundaries are rejected as ambiguous. Intentional
overlap across different project/group scopes is allowed.

The registry is parsed as the first step of application runtime composition,
before logging, global resolver-policy parsing, database or storage
construction, clients, caches, working directories, credential access, or
subprocess owners. Invalid input therefore aborts startup with a static
diagnostic and without echoing rejected configuration. JSON duplicate keys,
unknown fields, non-standard numbers, controls, and invalid UTF-8 text fail
closed. The raw value is bounded independently to 24,576 Unicode code points
and 24,576 UTF-8 bytes, decoded JSON nesting is capped at depth 8, and at most
64 grants are accepted.

Every worker keeps the one immutable registry snapshot built at startup; no
request or background task rereads the environment. Changing or revoking a
grant requires restarting **all** workers that might retain the previous
snapshot. A rolling deployment does not complete revocation until the last old
worker has stopped.

Global resolver settings remain independent, conjunctive outer ceilings:
`LAB_TRACKER_RESOLVER_ALLOWED_ROOTS`,
`LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES`,
`LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS`,
`LAB_TRACKER_RCLONE_ALLOWED_REMOTES`, and
`LAB_TRACKER_GIT_ALLOWED_REMOTES` can further restrict a scoped grant, but they
never create one or widen one. Project and group roles likewise never
manufacture host, network, credential, or subprocess authority.

This registry slice defines and composes the typed grant snapshot but does not
yet enforce it at registration or use time; existing registered-store behavior
still depends on the global policies above. The following authority slices bind
registration and revalidate persisted bindings. During that staged integration,
a local grant's lexical proof is registration-only and must produce an opaque
denial at the I/O boundary until the retained-handle filesystem slice carries
and revalidates the selected grant. Contributor-authored direct paths, URLs,
rclone targets, and Git remotes remain inert metadata.

### Local filesystem policy

Local artifact resolution and registered `local_fs` store health share one
operator authority:

- `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS`: a list of host-local roots separated by
  `os.pathsep` (`:` on POSIX, `;` on Windows). An unset, empty, or
  whitespace-only value produces an explicit deny-all policy in the application
  runtime. Empty or whitespace-only components are omitted. Other components
  retain their exact spelling rather than being trimmed.
- `LAB_TRACKER_RESOLVER_RECOVERY`: enable read-only content-hash recovery for a
  moved or renamed local artifact after its original path returns a clean
  missing result (default: `false`). Accepted values are the normal explicit
  boolean spellings; an unrecognized value fails startup.
- `LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES`: maximum unique candidate-file
  identities returned by one recovery scan (default and hard maximum: `4096`;
  minimum: `1`).
- `LAB_TRACKER_RESOLVER_RECOVERY_MAX_DIRECTORIES`: maximum root/child-directory
  attempts admitted by one recovery scan (default and hard maximum: `4096`;
  minimum: `1`).
- `LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES`: cumulative accepted full-file
  payload allowance across one logical registered-store attempt and every recovery
  candidate (default and hard maximum: `536870912`, 512 MiB; minimum: `1`).
  The compatibility name refers to recovery, but this is also the registered
  local payload ceiling. The helper may read one additional byte only as a fatal EOF
  proof; that byte is discarded and terminates resolution. This setting is
  separate from the request's `max_bytes`, which controls only the returned
  view and remains capped at 8 MiB.

Operator roots preserve the useful configuration semantics without inspecting
their filesystem targets: the current-user `~` form is expanded only from
`HOME` on POSIX or `USERPROFILE`/`HOMEDRIVE`+`HOMEPATH` on Windows; named-user
forms are rejected so expansion never invokes an NSS or account lookup.
Relative entries are prefixed with the service process's startup working
directory. Use absolute paths in deployments so a working-directory change
cannot change the grant. The resulting spelling must be unambiguous: dot or
dot-dot components, NUL/control characters, and unsupported platform
namespaces fail startup. POSIX repeated separators are rejected; Windows
normalizes only slash direction plus redundant or trailing separators, which
are native spelling aliases. A registered store root must be a native absolute
local path, and a grant to one of its children cannot partially authorize the
broader store. When recovery is enabled, startup also proves that the complete
configured root set plus a worst-case portable target name fits the broker's
single fixed-size helper request, and that every individual root can carry at
least a one-component candidate read at the hard byte allowance. An oversized
aggregate or individually unusable root set fails startup instead of turning
every recovery into an opaque runtime failure.

Health admission recognizes the configured lexical root spelling. If that root
is itself an operator-installed alias, a registered store written with the
alias's separate physical spelling is denied unless that spelling is configured
as another root. This conservative rule lets a lexically disjoint candidate
return without probing filesystem targets. Alias components inside an admitted
candidate remain eligible when the bounded helper proves their destination is
inside the selected grant.

Application composition builds one filesystem-I/O-free
`LocalFilesystemAuthority` inside one bounded local-filesystem operations
broker and shares one bounded process executor. The runtime retains the broker,
not a parallel authority or path policy. Local-store health, registered local
artifact reads, recovery enumeration, and every recovery candidate read receive
that exact broker. Candidate authorization, alias traversal, enumeration, open,
regular-file validation, and byte reads therefore occur in the isolated helper,
not in the application process.

Recovery is one helper-owned, pre-follow-safe traversal under the logical
resolution deadline. A registered recovery first retains its store root as a
nested non-popable boundary. Every root or child-directory attempt consumes the
directory ceiling before identity deduplication; duplicate aliases and cycles
are not enumerated twice, but cannot evade the cap. The helper admits no more
than the configured number of unique candidate-file identities or directory
attempts and emits only bounded path-free locator metadata in one response
capped at 8 MiB. It may keep classifying entries under the shared deadline to
find and promote an original-basename alias, but never descends into work that
the directory ceiling did not admit. Before exposing a candidate, the broker
also proves that its exact subsequent retained-root read request fits the fixed
24 KiB request envelope. An otherwise valid locator that cannot fit is omitted
and changes the result to an explicit limit rather than poisoning the logical
read budget. A ceiling or metadata omission produces the same limit result,
which cannot be mistaken for an exhaustive scan; if its bounded candidates do
not verify, recovery fails terminally. Malformed output, deadline expiry
(including response validation), traversal ambiguity, cleanup failure, or an
unsupported namespace discards the candidate set and fails closed.

#### Mount and namespace authority

An allowed root grants the transitive subtree visible beneath that path in the
service's host or container filesystem namespace. It is a namespace grant, not
a grant to one device, filesystem, or volume identity. The supported cases are:

| Namespace case | Decision |
| --- | --- |
| POSIX ordinary mount beneath an allowed root | Allowed |
| Linux bind mount beneath an allowed root | Allowed |
| Supported Windows drive-letter anchor | Allowed and trusted for that operation |
| Windows nested volume mount point | Unsupported; fail closed |
| Windows UNC, device, or GUID-volume namespace | Unsupported; fail closed |
| Symlink or junction alias proven to resolve inside the same grant | Allowed as an alias |
| Escaping or ambiguous name-surrogate alias | Denied |
| Directory-capable, non-name-surrogate Cloud Files placeholder | Eligible; not a mount crossing |

Consequently, POSIX traversal must not reject a descendant merely because it
crosses a device boundary: `RESOLVE_NO_XDEV`, `st_dev` equality, and similar
checks would incorrectly revoke an allowed ordinary or bind mount. On Windows,
the configured drive mapping is part of the trusted deployment boundary.
Network mappings that normalize outside the supported drive namespace, nested
volume mount points, and unsupported final namespaces fail closed. Symlinks and
junctions do not add authority; an operation may use one only after its bounded
resolver proves that the destination remains inside the same root.

The deployment operator is trusted to control this setting, the service mount
namespace, bind and FUSE mounts, container volume mappings, and Windows DOS
device mappings. API users and ordinary data writers are not trusted to mutate
that topology. If an untrusted principal can change mount, FUSE, device-map, or
volume-mapping topology beneath an allowed root, local artifact resolution,
recovery, and health are unsupported and must be disabled or isolated from that
principal.

Mount and device-map changes are deployment changes and must not occur during a
filesystem operation. Each operation observes a point-in-time namespace.
Retained descriptors and handles bind the selected objects for that operation;
they do not create a lease over later namespace state, and store health remains
only a point-in-time reachability result. See
[`self-hosted-operations.md`](self-hosted-operations.md#local-filesystem-stores)
for deployment guidance.

Runtime composition parses these settings once into typed `Settings` and one
shared broker/executor pair. The broker exclusively owns the frozen, slotted
authority used by health, reads, and enumeration; no `LocalPathPolicy` is
retained by the application runtime. Concrete resolver methods remain trusted
library primitives for explicitly constructed adapters. The public
`ResolverRegistry.resolve()` facade, including the registry returned by
`default_registry()`, refuses raw references; application and MCP resolution
dispatch only sealed, project-authorized registered-store targets.

#### Bounded local artifact reads

One `LocalResolutionBudget` is created for a logical direct or registered-store
resolve. It owns one absolute
`LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS` deadline and the cumulative
`LAB_TRACKER_RESOLVER_RECOVERY_MAX_BYTES` allowance. The same identity-stable
budget is reused for the direct attempt and all recovery candidates. A clean
missing or denied attempt that emitted no bytes releases its reservation.
Programmatic composition that supplies both `RecoveryPolicy.max_bytes` and
`LocalResolutionLimits.max_read_bytes` must use the same value; conflicting
limits are rejected rather than silently choosing one.
Successful reads debit their exact payload length. Timeout, malformed protocol,
unexpected output, partial read, metadata change, cleanup/process failure, or
any other ambiguous outcome consumes the remaining allowance and makes the
logical budget terminal; resolution returns a static, path-free failure.

The helper retains the authorized file descriptor or handle from traversal
through hashing output. It takes before/after metadata snapshots, reads exactly
the size reported by the first snapshot, then performs one bounded one-byte EOF
read. The process boundary permits at most the current allowance plus that
single proof byte on raw stdout. A proof byte is always fatal and is never
accepted as payload. A stable file whose size equals the allowance succeeds
when the EOF read is empty; a larger preflight size fails without being read.
These checks do not provide snapshot isolation and do not promise detection of
every same-size concurrent rewrite. The full content hash remains the integrity
gate for the bytes observed.

Enumeration runs in the same contained helper-process boundary as reads. The
shared absolute deadline covers root admission, the single traversal, candidate
reads, and verification; expiry terminates the helper and makes the logical
budget terminal. Subprocess containment cleanup may additionally use the
executor's fixed cleanup grace. Operators must still treat mount and device-map
topology as a trusted deployment boundary rather than a tenant-controlled input.

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

New HTTP registrations store their canonical directory prefix in `root` and
reject every present `endpoint`; they also reject embedded credentials.
Historical rows may still contain `endpoint`. Health checks preserve the legacy
interpretation exactly: a present endpoint is authoritative, including when it
is blank or invalid, and never falls back to `root`. The selected initial URL
must pass the hardened registered-base structural grammar before host I/O. The
health probe sends `HEAD` through the same
outbound policy, pinned client, and total deadline as HTTP artifact resolution.
Statuses `301`, `302`, `303`, `307`, and `308` are followed manually while
preserving `HEAD`; every hop is reauthorized and repinned, safe cross-origin
redirects may proceed, and an HTTPS-to-HTTP downgrade is denied. A terminal
`2xx`, `403`, or `405` response counts as reachable. Policy denials, redirect
loops or limit exhaustion, transport/deadline failures, and other terminal
statuses all return the same static redacted health detail.

Local-store health is a bounded, read-only reachability hint, not registration
validation or a durable filesystem capability. Registration performs no host
I/O. For an explicit health request, the probe creates one absolute process
deadline before invoking its directory-inspection role. The broker then
validates the native absolute candidate and selects the most-specific
operator grant using side-effect-free component comparison. Deny-all,
malformed, lexically disjoint, and sibling-prefix candidates return one static
failure without starting a child or touching the candidate filesystem target.

An admitted request is checked by a fixed isolated Python helper through the
same bounded process executor used by rclone and Git. A compact, versioned,
size-bounded ASCII JSON environment value carries only the lexically admitted
candidate and its selected grant. POSIX spelling is preserved; Windows
normalizes only drive letter, slash direction, and redundant separators before
the strict helper protocol. Neither path appears in argv, output, or an
exception. The remaining environment is limited to Python's required platform
bootstrap and locale variables. The helper emits no stdout or stderr.

Inside the deadline, the helper first resolves and retains the operator root as
the trusted grant anchor. It then walks the candidate suffix one component at a
time relative to retained directory descriptors or handles. On POSIX,
no-follow metadata and `readlinkat` parse link text before any target component
is opened; normal directories use no-follow `openat`, exact-descriptor `fstat`,
and effective search checks. Relative and absolute alias targets are rewritten
inside the same retained grant, and dot-dot pops the retained descriptor stack
rather than being normalized over an unresolved link. On Windows, each
component is opened no-follow relative to the retained preceding handle.
Symlink and mount-point reparse payloads are read from that exact handle,
strictly parsed, and rewritten only after their target is proven to remain in
the same drive-root grant. A junction targeting an in-grant DOS path is
eligible; nested volume-GUID mounts, UNC/device/GUID namespaces, malformed
payloads, and escaping name surrogates fail before target traversal.
Directory-capable non-name-surrogate Cloud Files placeholders remain eligible
parents.

Search-only POSIX directories remain eligible; an unsearchable directory fails
closed. POSIX uses `O_SEARCH`, `O_PATH`, or the equivalent directory use of
`O_EXEC`; it never falls back to a read-requiring `O_RDONLY` open. A macOS
compatibility branch supplies the public Darwin `O_EXEC` ABI bit when an older
CPython build omits that symbolic constant. Explicit helper-owned descriptor
and handle cleanup is best effort, with contained helper exit as the backstop
for failed closes and asynchronous interruption windows.

The one deadline covers broker admission and serialization, interpreter
startup, trusted-root anchoring, alias resolution, opens and validation,
process exit, and output drainage and is checked again after the executor
returns. Executor termination, kill, and reap retain their separate fixed
cleanup grace. Only the accessible exit with zero output is healthy. Timeout,
containment failure, denial, operational failure, unknown exit, output, or any
ordinary adapter error returns the same static detail. Adapter-level
`BaseException` still propagates after executor-owned cleanup. Health is a
point-in-time result about the exact directory object retained when validation
completes, not a durable capability or lease. Mount crossings follow the
namespace-transitive authority above.

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

### Subprocess-backed artifact resolution

Local reads execute a fixed Python helper, while rclone and Git adapters execute
optional host binaries, through the shared bounded process executor. The
configured budget is one monotonic deadline for the entire logical operation:
a local direct read and all of its recovery candidate reads share one deadline;
rclone metadata lookup, transfer, and verification share one deadline; and Git
fetch, object inspection, transfer, and verification share one deadline. A
local, rclone, or Git store-health probe receives a fresh deadline; Git's URL
preflight and HEAD query share it. Progress, recovery, or moving between
subprocesses does not reset it. Local health creates the deadline before lexical
admission; local artifact resolution creates it once for the logical read. Both
pass the exact deadline object through the bounded filesystem broker.

- `LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS`: execution and verification
  budget for one local, rclone, or Git artifact resolution, or one local,
  rclone, or Git store-health probe (default: `30`). The value must be finite,
  greater than zero, and no greater than `86400` seconds (one day); invalid
  values fail application startup. This setting is independent of
  `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS`.
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

The local root list, local recovery controls, and rclone and Git policies are
parsed once from `Settings` at startup; no runtime consumer independently
rereads the process environment. Runtime builds one local operations broker
from that root list and passes the exact broker to health, artifact resolution,
and bounded recovery enumeration. Rclone and Git resolution and health share
one immutable instance of their corresponding policy. All subprocess-backed
adapters share one bounded process executor. New rclone-backed registrations
accept a decoded path within a remote and preserve the distinction between
`remote:path`, `remote:/path`, and `remote:/`; an `s3://bucket` URL is not a
registered rclone root. A present `credential_ref` must be one exact rclone
remote name, while absence uses the store name. Historical rows can still
contain a blank or invalid `credential_ref`; at health and resolution boundaries
its presence remains authoritative and never falls back to the store name. The
bounded `rclone lsf` intentionally reports a large/noisy root as unreachable
when fixed metadata output limits are exceeded.

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

Every subprocess receives independent stdout and stderr memory caps. Local raw
file output is capped at its remaining logical allowance plus the one EOF-proof
byte described above. Rclone and Git artifact bytes are streamed and checked
against the resolver's `max_fetch_bytes` limit as they arrive; their preflight
size is advisory and cannot permit a growing object to exceed that limit.
Timeout, output overflow, malformed metadata, or failed cleanup produces a
generic unresolved or adapter-specific unreachable result without exposing a
remote, path, credential, exception, or raw stderr. Pipes are closed and an
uncooperative process is terminated, then killed and reaped within a separate
fixed cleanup grace. A failed call can therefore exceed the configured
execution deadline only by that bounded cleanup grace.

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
