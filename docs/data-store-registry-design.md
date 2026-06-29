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
exactly one is the **default**. Registration runs a **health check** (can I stat
the root / list the prefix / open a connection with the host's credentials?), so
a misconfigured store fails *at registration*, not three weeks later when an
agent tries to resolve an artifact.

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

`ExternalArtifactReference` gains an optional `store_id` and an in-store
`locator`; the absolute `uri` becomes **derived per host** by the store adapter,
not the primary key. `content_hash` remains the stable cross-store, cross-machine
identity — the same join key `build-vs-buy-boundaries.md` already relies on.

### Back-compat with today's references

`ExternalArtifactReference` is unchanged for existing rows: a legacy reference
with a free-form `source_system` (`s3`, `mlflow`, `doi`, `datalad`…) and an
absolute `uri` still resolves through the generic local/http/kind adapters from
the resolver design. New captures **prefer** store-relative locators; a
`source_system` that names a registered store binds to it, and an unregistered
one falls back to best-effort generic resolution. No migration is forced.

## Resolution flow (extends the resolver design)

This slots directly into
[`external-artifact-resolution-design.md`](external-artifact-resolution-design.md);
the store registry is what its `ResolverRegistry` dispatches through:

1. Reference → `store_id` → look up the `DataStore` (kind, capabilities,
   endpoint, host-local credential ref).
2. Pick the adapter for that `kind`; assert the operation's required capability
   (e.g. a byte-range read needs `BYTE_RANGE`; a query needs `QUERY`).
3. Adapter resolves the in-store locator → bounded bytes / bounded result set.
4. Verify against `content_hash` → tri-state `verified` / `drifted` /
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

Shipped (project-scoped slice):

- ✅ `DataStore` domain model + `StoreKind`/`StoreCapability` enums, the
  `data_stores` table, and Alembic migration `0049_data_stores`. Project-scoped;
  group scope is a deferred additive column. Capabilities default from the store
  kind (`default_store_capabilities`).
- ✅ `SQLAlchemyDataStoreRepository` (CRUD + `query`/`get_by_name`/`get_default`/
  `clear_default`, at most one default per project), `DataStoreService` with
  contributor/read RBAC and unique-name-per-project (`ConflictError`), and the
  `LabTrackerAPI` facade.
- ✅ Routes `POST /data-stores`, `GET /data-stores`, `GET /data-stores/{id}`.
- ✅ `store://<name>/<path>` resolution: `store_relative_reference` translates a
  locator into a concrete reference (`local_fs → file://`, `http → URL`, and the
  rclone kinds → `rclone://` with the remote from `credential_ref`), and the
  resolve endpoint materializes it via `data_stores.get_by_name` before
  dispatching to the resolver. Credentials are never embedded.

Deferred:

- ⏭️ Group-scoped stores (additive `group_id` column + inheritance).
- ⏭️ The capabilities the storeless adapters cannot provide: `versioned_snapshot`
  reads (S3 `versionId`, Iceberg/Delta) and the `database` (`query → rows`)
  adapter — `store_relative_reference` returns `None` for `object_table`/
  `database` today, surfacing as a clean `UNRESOLVED`.
- ⏭️ Registration health check; a `store_id`+`locator` form on
  `ExternalArtifactReference` (the current locator travels in `uri`).

## Suggested first slice

1. `DataStore` model + migration; project/group scoping and a single default;
   a registration health check.
2. Store-relative `store://name/...` locator parsing; optional `store_id` +
   `locator` on `ExternalArtifactReference` with legacy fallback intact.
3. Adapters: `local_fs`, `s3` (version-aware), `ssh`, and an `rclone` adapter
   covering Dropbox/GDrive/Box/OneDrive; a `database` adapter behind the
   snapshot/`unversioned` semantics with a hard row cap.
4. Resolution dispatches through the registry (extends the resolver first slice).
5. Capture clients write store-relative references against the default store.
6. Tests: registration health check; `verified` local/S3 read; `drifted` on a
   mutated object; S3 `versionId` pin; `unversioned` DB read; capability
   mismatch (range read on a no-range store) → clean `unresolved`.

## See also

- [`external-artifact-resolution-design.md`](external-artifact-resolution-design.md)
  — the on-demand resolver this registry dispatches through.
- [`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md) — byte/asset storage
  boundary, the content-hash join key, credential-offload, and anti-scope.
- [`experiment-walkthrough-coverage.md`](experiment-walkthrough-coverage.md) and
  [`lab-experiment-documentation.md`](lab-experiment-documentation.md) — the
  multi-machine → single-store reality that motivates a declared store.
