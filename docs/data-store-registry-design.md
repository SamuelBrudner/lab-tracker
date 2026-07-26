# Data Store Registry — Design

## The opinion

Lab Tracker is **not** your data store, and never will be (see
[`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md): byte durability,
transfer, lifecycle, and cross-workstation availability belong to object stores
and data substrates). But Lab Tracker is **opinionated that you have one — and
that you declare it.**

> You have a durable place where your data lives. Register it with Lab Tracker
> once. From then on, every artifact is addressed *relative to a registered
> store*, not by a raw machine path.

This is the structural fix for everything the previous two designs worked
around: the per-machine OneDrive path problem, the "core facility computer can't
run `lt watch`" gap, and the dead-pointer problem in
[`external-artifact-resolution-design.md`](external-artifact-resolution-design.md).
A reference stops being a brittle absolute path on whoever's laptop captured it
and becomes `store + in-store locator + content_hash`, which resolves on any host
that can reach the store.

### Why be opinionated about this

- **It keeps Lab Tracker out of byte storage** while making resolution reliable.
- **References become portable by construction.** Declare the store once;
  every reference is relocatable and cross-machine, because the host-specific
  part (mount root, endpoint, bucket) lives on the *store*, not the artifact.
- **One integrity story.** Every artifact has a content hash relative to a known
  store, so `verified` / `drifted` means something.
- **It matches what good labs already do.** The scientist in the walkthrough
  already funnels everything into one OneDrive; the opinion just names that
  habit and makes the tool depend on it instead of guessing paths.

## Location is declared in Lab Tracker; credentials stay host-local

The single most important separation in this design:

| Lab Tracker owns | The host / user owns |
| --- | --- |
| *Where* the store is: kind, endpoint, bucket/root, scope, a stable name | *How* to authenticate to it: OAuth token, SSH key, AWS profile, DB password |
| A **credential reference** (a name/handle), never a secret | The actual secret, in env / OS keychain / secret manager / `rclone` config / `ssh-agent` |

A `DataStore` registration says "the lab archive is the S3 bucket
`s3://lab-archive`, or the OneDrive remote `lab-onedrive`." It does **not** carry
the access key or the OAuth token. Each host/user supplies access the way their
OS and tools already do. This keeps Lab Tracker from becoming a secret store or
an identity provider (explicit anti-scope in `build-vs-buy-boundaries.md`), and
it lets a *shared* group store be declared once while each member authenticates
as themselves.

## The `DataStore` entity

A new first-class, configured registration (project- or group-scoped, mirroring
the existing `ProjectGroup(kind=lab)` / `group_read_all` inheritance):

```python
class StoreKind(str, Enum):
    LOCAL_FS = "local_fs"          # a path on this host (incl. a synced cloud folder)
    SSH = "ssh"                    # SFTP/scp to a server
    S3 = "s3"                      # S3 / MinIO / any S3-compatible
    GCS = "gcs"
    AZURE_BLOB = "azure_blob"
    DROPBOX = "dropbox"
    GDRIVE = "gdrive"
    BOX = "box"
    ONEDRIVE = "onedrive"
    OBJECT_TABLE = "object_table"  # lakehouse table format: Iceberg / Delta / Hudi
    DATABASE = "database"          # SQL / warehouse: Postgres, BigQuery, Snowflake, DuckDB
    HTTP = "http"                  # read-only public/authenticated URLs

class StoreCapability(str, Enum):
    BYTES_BY_PATH = "bytes_by_path"        # key/path -> bytes
    BYTE_RANGE = "byte_range"              # partial reads
    LIST = "list"                          # enumerate under a prefix
    VERSIONED_SNAPSHOT = "versioned_snapshot"  # stable as-of id (S3 versionId, Iceberg snapshot, Delta version)
    QUERY = "query"                        # selector -> result rows

class DataStore(_DomainModel):
    store_id: UUID
    project_id: UUID | None          # project-scoped, or...
    group_id: UUID | None            # ...group(lab)-scoped, inherited like group_read_all
    name: str                        # stable handle used in locators, e.g. "lab-onedrive"
    kind: StoreKind
    capabilities: list[StoreCapability]
    root: str                        # bucket / prefix / base path / DB+schema
    endpoint: str | None = None      # host, region, API base — when not implied by kind
    credential_ref: str | None = None  # a NAME, resolved host-side; never a secret
    is_default: bool = False         # the opinionated default for new artifacts
    # provenance/audit fields mirror other entities (created_by, created_at, ...)
```

A project's **effective store set** = its own stores plus inherited group stores;
at most one is the **default** (a scope may have none). Registration validates
stored configuration without initiating backend I/O or invoking a health
adapter. Operators can run the separate, read-only health endpoint for a bounded
advisory check of a local root, remote prefix, or configured credentials. A
healthy result describes reachability at probe time; it is not a registration
guarantee or a durable capability.

## Backends differ by capability, not brand

The user's list — data lakes, databases, SSH servers, Dropbox, Google Drive,
Box, OneDrive — is not one shape. Dispatching on `capabilities` instead of `kind`
is what keeps the resolver honest.

| Store | Addressing unit | Byte range | Versioned snapshot | "verified" hash is meaningful? | Credential mechanism |
| --- | --- | --- | --- | --- | --- |
| local_fs (incl. synced cloud folder) | path | yes | no | yes (immutable file) | host filesystem perms |
| ssh / sftp | path | yes | no | yes | SSH key / `ssh-agent` |
| s3 / gcs / azure_blob | key | yes | **yes** (versionId) | yes | cloud profile / IAM |
| dropbox / gdrive / box / onedrive | path or file-id | partial | rev/version id | yes (pin the rev) | OAuth (host-side) |
| object_table (Iceberg/Delta/Hudi) | table @ snapshot | n/a | **yes** | yes (snapshot is immutable) | underlying object store |
| database (live tables) | query / table @ as-of | n/a | only if temporal/versioned | **only against a snapshot** | connection string ref |
| http | URL | yes (Range) | no | yes if content-addressed | none / bearer |

### The two hard cases the matrix forces into the open

- **Databases.** There is no file to fetch — the artifact is a *query result*.
  A database-backed reference must record the **selector** (table or query) plus
  a **snapshot anchor** (a temporal `AS OF`, a warehouse snapshot id, or, failing
  that, the *result digest computed at capture time*). Resolution re-runs the
  selector as-of that anchor and re-verifies. Against a **mutable** table with no
  versioning, `verified` is impossible by definition — resolution returns
  `unversioned` with the current bounded result and says so, rather than
  pretending the rows match what the claim was built on. Always bound the result
  (`LIMIT` / row cap).
- **Data lakes.** Split them: a *raw-file* lake (parquet/CSV in object storage)
  is just `BYTES_BY_PATH`; a *table-format* lake (Iceberg/Delta) is
  `VERSIONED_SNAPSHOT` and resolves cleanly because the snapshot id makes the
  read immutable. Pin the snapshot/version in the locator so a later resolve is
  reproducible.

## Addressing: store-relative locators

The reference stops carrying an absolute, host-specific URI as its identity.
Instead:

```
store://<store-name>/<path-or-key>[@<version-or-snapshot>][?<selector>]
```

- `store://lab-onedrive/experiments/001/flow/sample.fcs`
- `store://lab-archive/runs/2026-06-28/raw.h5@v3`    (S3 versionId / Iceberg snapshot)
- `store://lims/?q=SELECT … WHERE plate='001'&as_of=2026-06-28T12:00Z`  (database)

`ExternalArtifactReference` carries an optional `store_name` and an in-store
`locator`; the absolute `uri` becomes **derived per host** by the store adapter,
not the primary key. `content_hash` remains the stable cross-store, cross-machine
identity — the same join key `build-vs-buy-boundaries.md` already relies on.

For `local_fs`, the locator is a strict portable relative path, not a fragment
that is normalized after joining. It is parsed into immutable components before
any path or filesystem operation. Absolute, drive/UNC/device, backslash,
empty/dot/dot-dot, control, reserved-device, and encoded separator/traversal
forms are rejected rather than repaired. The structured `store_name`/`locator`
fields and the logical `store://` URI must describe one canonical identity.
Local-store names use 1–63 ASCII letters, digits, dots, underscores, or hyphens
and start with a letter or digit. New registrations reject other names; legacy
local rows with names or roots outside this contract fail closed at resolution.
The `for_local_store(...)` constructor creates this canonical identity.

The generic `for_store(...)` constructor remains a legacy compatibility surface.
Kind-specific constructors and materialization boundaries enforce the portable
grammar for local, HTTP, rclone, and registered Git references.

### Back-compat with today's references

`ExternalArtifactReference` is unchanged for existing rows: a legacy reference
with a free-form `source_system` (`s3`, `mlflow`, `doi`, `datalad`…) and an
absolute `uri` still resolves through the generic local/http/kind adapters from
the resolver design. New captures **prefer** explicit `store_name`/`locator`
fields or a `store://` URI. Legacy source-system labels remain compatible; they
do not independently name or authorize a registered store. No migration is
forced.

## Resolution flow (extends the resolver design)

This slots directly into
[`external-artifact-resolution-design.md`](external-artifact-resolution-design.md);
the store registry is what its `ResolverRegistry` dispatches through:

1. Reference → store name → look up the `DataStore` (kind, capabilities,
   endpoint, host-local credential ref).
2. Validate and detach a typed store target while the database scope is open,
   then release that scope before host I/O.
3. Pick the adapter for that `kind`; assert the operation's required capability
   (e.g. a byte-range read needs `BYTE_RANGE`; a query needs `QUERY`).
4. Adapter resolves the in-store locator → bounded bytes / bounded result set.
5. Verify against `content_hash` → tri-state `verified` / `drifted` /
   `unresolved`, plus `unversioned` for mutable-DB reads that cannot be certified.

The bounding, untrusted-content handling, RBAC gating, and optional
content-addressed cache from the resolver design all carry over unchanged.

## Credentials, concretely: lean on a unifier, don't build one

Writing native OAuth + transfer code for Dropbox, Google Drive, Box, and OneDrive
is exactly the "credential/session plumbing" `build-vs-buy-boundaries.md` says to
**offload, not own**. Recommended stance:

- **Default unifier: `rclone`.** One mature tool already speaks Dropbox, Google
  Drive, Box, OneDrive, S3, GCS, Azure, SFTP, and more, with its own credential
  store. A single `RcloneResolver` adapter — `credential_ref` = an rclone remote
  name — covers most of the user's list at once, and the secrets live in
  `rclone.conf` on the host, never in Lab Tracker.
- **Native adapters where it pays:** `local_fs` (no dependency), `s3`
  (ubiquitous, version-aware), `ssh` (`paramiko`/`asyncssh`), and `database`
  (a thin DB-API/SQLAlchemy read path with a hard row cap). These earn native
  code because of versioning, range reads, or query semantics rclone doesn't model.
- **Lab Tracker owns neither tokens nor refresh flows.** It stores the store
  definition and a credential *reference*; the host resolves it.

### Cross-platform, including Windows

The lab is partly on Windows (see
[`windows-fresh-clone.md`](windows-fresh-clone.md)), so the unifier must not be
Unix-only:

- **`rclone` is native on Windows** — a single static Go binary (`rclone.exe`,
  amd64/386/arm64), no WSL/Cygwin/MSYS required. Its OAuth flows and config
  (`%APPDATA%\rclone\rclone.conf`) work the same across Windows, macOS, and
  Linux. This is the reason to prefer it over **`rsync`, which is *not* native on
  Windows** (it needs WSL/Cygwin/cwRsync). The design uses rclone, never rsync.
  Lab Tracker's bounded rclone/Git process owner contains descendants with a
  dedicated process group on POSIX and a kill-on-close Job Object on Windows.
  The Windows leader remains suspended until Job Object assignment has been
  verified, so inability to establish secure containment fails closed before
  artifact resolver code executes.
- **The common Windows case needs no rclone at all.** OneDrive on a Windows
  workstation is normally a *synced local folder*, so it resolves through the
  plain `local_fs` adapter with zero credentials. rclone is only the fallback
  for stores that are **not** locally mounted (a headless server, an HPC node, or
  an agent running where the drive isn't synced).
- Native adapters are equally portable: `local_fs`, `s3`, and `database` are pure
  Python; `ssh` via `paramiko`/`asyncssh` is cross-platform.

### Decision (locked)

**rclone-first for the cloud drives, native adapters for S3 / SSH / database /
local_fs.** rclone is the single adapter for Dropbox, Google Drive, Box, and
OneDrive (when not locally synced); the four native adapters earn their own code
because of versioning, byte-range, query, or zero-dependency needs that rclone
doesn't model. Native-everywhere was rejected: more code and more credential
surface for marginal gain on the cloud drives, and it duplicates plumbing
`build-vs-buy-boundaries.md` says to offload. This is the design's committed
adapter strategy, not an open question.

## Capture writes store-relative references

Closing the loop with the capture clients (`lt watch`, `lt hpc`, figure capture):
once a project has a default store, capture records artifacts as
`store://<default>/…` locators with content hashes, instead of bare local paths.
The shared-core-PC gap (G7 in
[`experiment-walkthrough-coverage.md`](experiment-walkthrough-coverage.md))
softens: even a manual upload to the lab OneDrive becomes resolvable the moment
its store-relative locator + hash are recorded, with no agent on that machine.

## Anti-scope (unchanged boundaries)

Lab Tracker still does **not**: store bytes, manage object-store lifecycle /
replication / transfer, own OAuth identity or secrets, build a load-by-name data
catalog, or run a pipeline/runner. The registry is *configuration* — "where are
your stores" — plus thin, optional adapters. That is precisely what
`build-vs-buy-boundaries.md` permits under "Adapter configuration" and "Stable
external URI and content-hash storage."

## Implementation status

Shipped:

- ✅ `DataStore` domain model + `StoreKind`/`StoreCapability` enums, the
  `data_stores` table, and Alembic migrations `0049_data_stores` and
  `0050_data_store_group_scope`. Stores may be project- or group-scoped;
  capabilities default from the store kind (`default_store_capabilities`).
- ✅ `SQLAlchemyDataStoreRepository` (CRUD + `query`/`get_by_name`/`get_default`/
  `clear_default`, at most one default per project), `DataStoreService` with
  contributor/read RBAC and unique-name-per-project (`ConflictError`), and the
  `LabTrackerAPI` facade.
- ✅ Routes `POST /data-stores`, `GET /data-stores`, `GET /data-stores/{id}`.
- ✅ `store://<name>/<path>` resolution: the resolve endpoint authorizes and
  looks up the store before releasing its database scope. `local_fs` becomes a
  typed target that retains the logical URI, validated relative components, and
  trusted registered root through the eventual handle-bound read. HTTP becomes
  a typed target that retains the same portable relative components and its
  canonical registered origin/path prefix through every redirect. Rclone kinds
  become a typed target that retains an exact configured remote, structural
  rooted-versus-relative prefix, and portable locator until argv composition.
  Git becomes a typed target that retains a structurally parsed remote, portable
  repository path, and full immutable object ID. Credentials are never embedded.
- ✅ Explicit health control plane: `GET /data-stores/{id}/health` has
  independent no-wait admission, performs opaque authorization before every
  cache access, detaches a frozen exact-value probe target, and releases the
  ordinary request database scope before cache or host I/O. Completed results
  use a hard-bounded process-local TTL/LRU cache with exact-key single-flight
  and bounded follower waits. HTTP health treats a present `endpoint` as
  authoritative (including blank or invalid values), never falls back to
  `root`, and requires the selected initial URL to pass the hardened
  registered-base structural grammar before host I/O. It sends a bounded
  `HEAD` through the exact outbound policy and pinned client used by artifact
  resolution, reauthorizes and repins every redirect under one total deadline,
  and returns one static failure detail. Rclone and Git use dedicated adapters
  over the exact immutable remote policies and bounded process executor shared
  with artifact resolution. Rclone preserves `remote:path`, `remote:/path`, and
  `remote:/` while running one fixed bounded `lsf`; Git retains exact URL
  preflight, redirect denial, sanitized environment, app-owned working
  directory, and one deadline across both bounded commands. Invalid targets
  perform no per-probe process work, and ordinary failures expose only one
  static detail per adapter. Local health first restricts the object-identical
  operator `LocalPathPolicy` to the complete registered root, then invokes one
  fixed isolated, output-free Python helper under the shared process executor
  and subprocess deadline. The platform helper opens each canonical component
  relative to the retained preceding no-follow directory handle, requires
  search/traverse permission, and validates the final handle, preventing helper
  inspection from being redirected by a link or junction substitution race.
  Helper-owned close attempts are best effort and contained helper exit is the
  cleanup backstop. Policy narrowing and canonicalization occur in the parent
  before that deadline. This remains a static, advisory point-in-time result
  rather than a durable filesystem lease. The legacy helper now fails closed
  for local, HTTP, rclone, and Git. `object_table` and `database` remain
  unsupported.
- ✅ Group-scoped stores: a store is scoped to exactly one of a project or a
  group (migration `0050`, nullable `project_id` + `group_id`). A group store is
  inherited by every project in the group — `get_by_name` resolves a project's
  own store first, then its group's, so `store://` resolution inherits with no
  endpoint change. Group stores require group-owner RBAC; a project's listing
  returns its effective (own + inherited) set.
- ✅ Structured field form: `ExternalArtifactReference` carries optional
  `store_name` + `locator` (paired) with a `for_store(...)` constructor, so a
  store-relative artifact has an explicit representation. For `local_fs` and
  HTTP, resolution accepts the fields only when they agree with the canonical
  logical `store://` identity; `for_local_store(...)` and
  `for_http_store(...)` construct their canonical forms, and both kinds share
  one immutable portable-path grammar. The generic `for_store(...)` constructor's
  deterministic legacy display URI remains accepted for structured HTTP, rclone,
  and Git references and is canonicalized during preparation. The specialized
  `for_git_store(...)` constructor produces a portable path plus full immutable
  object ID. The field is the store *name* (not a UUID), matching name-based
  resolution.
- ✅ Local-store confinement: a `local_fs` root must be a native absolute local
  path. Its effective read authority is conjunctive with the operator's global
  local-root policy, and the exact reader plus recovery walk are scoped to the
  registered store root. A broader global root therefore cannot make sibling
  files addressable through that store. Application composition parses
  `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS` once as an `os.pathsep`-separated
  operator list and shares one immutable policy with local resolution and local
  health; unset or empty runtime configuration denies every local root. The
  grant is namespace-transitive rather than device-bound; the trusted actor,
  platform matrix, and point-in-time lifecycle are defined by the normative
  [mount and namespace authority](configuration.md#mount-and-namespace-authority)
  contract.
- ✅ Registered HTTP prefix confinement: a pure value validates the canonical
  HTTP origin and portable path components without DNS. A frozen, factory-only
  target crosses the database-scope boundary, and the resolver checks the
  initial URL and every raw redirect before the next DNS or socket operation.
  Direct HTTP references retain their existing, broader redirect semantics.
- ✅ Registered rclone prefix confinement: typed remote names follow rclone's
  configured-name grammar, registered roots preserve `remote:path` versus
  `remote:/path`, and a frozen factory-only target crosses the database-scope
  boundary. Nominal dispatch composes one exact target token only after the
  combined root/locator budget and exact remote allowlist pass. Direct
  `rclone://` references retain their established parser and process behavior.
- ✅ Registered Git confinement: `GitObjectId` accepts only full lowercase,
  nonzero SHA-1 or SHA-256 IDs, and `PinnedGitPath` pairs that ID with a portable
  repository path. A frozen factory-only target carries the canonical logical
  identity and structurally parsed registered remote across the database-scope
  boundary. Nominal dispatch reauthorizes the remote before cache creation,
  separates cache namespaces by object format, and initializes Git explicitly
  with `--object-format=sha1` or `--object-format=sha256`. Direct generic
  `git+` behavior remains compatible pending its separate hardening.

Deferred:

- ⏭️ The capabilities the storeless adapters cannot provide: `versioned_snapshot`
  reads (S3 `versionId`, Iceberg/Delta) and the `database` (`query → rows`)
  adapter — `store_relative_reference` returns `None` for `object_table`/
  `database` today, surfacing as a clean `UNRESOLVED`.

## Next slices

The remaining work binds store registration to operator-approved authority
grants, enforces declared capabilities during resolution, and adds the deferred
snapshot/query adapters. Health remains an explicit operator action at
`GET /data-stores/{id}/health`, not a side effect of registration.

## See also

- [`external-artifact-resolution-design.md`](external-artifact-resolution-design.md)
  — the on-demand resolver this registry dispatches through.
- [`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md) — byte/asset storage
  boundary, the content-hash join key, credential-offload, and anti-scope.
- [`experiment-walkthrough-coverage.md`](experiment-walkthrough-coverage.md) and
  [`lab-experiment-documentation.md`](lab-experiment-documentation.md) — the
  multi-machine → single-store reality that motivates a declared store.
