# On-Demand External Artifact Resolution — Design

## Problem

Lab Tracker stores external artifacts as **pointers, not bytes**. An
[`ExternalArtifactReference`](../src/lab_tracker/models.py) is
`kind` + `source_system` + `uri` + `content_hash` + a compact `metadata`
snapshot, embedded on datasets (`manifest_external_artifacts`), analyses
(`external_artifacts`), and claims (`external_citations`). That is the correct
boundary — see [`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md): byte
durability, transfer, and cross-workstation availability belong to object
storage and data substrates, not to Lab Tracker.

Before this capability shipped, the pointer was **dead**: nothing in the
codebase dereferenced a reference to fetch its content. When an agent needed
content that was *never captured in the metadata snapshot* — the actual
sample→condition map inside `samples.xlsx`, the full text of an as-run protocol,
the values behind a plate map, a region of an `.fcs` file — the graph could tell
the agent *that* an artifact existed and *where*, but could not let the agent
*read* it.

For artifacts that reach a durable store (OneDrive, Box, S3, a DataLad remote),
Lab Tracker needs a way to **resolve the pointer on demand**: given a reference
already in the graph, fetch a bounded, hash-verified view of its content at
reasoning time.

## Scope and non-goals

This is a **read-only** capability and stays inside the established boundaries.

In scope:

- Dereference an existing `ExternalArtifactReference` on demand and return a
  bounded, integrity-checked view of its content.
- A pluggable, per-`source_system` adapter registry with a local-filesystem
  fallback — exactly the shape `build-vs-buy-boundaries.md` prescribes for byte
  and asset storage ("integrate behind a pluggable object-storage interface;
  keep local filesystem fallback").
- Expose resolution through the read-only assistant / MCP surface alongside the
  existing provenance read tools.

Explicitly **not** in scope (anti-scope from `build-vs-buy-boundaries.md`):

- No data catalog and no load-by-name. You resolve a reference that already
  exists in the graph, identified by its `content_hash`; you do not look
  artifacts up by name.
- No object-store lifecycle, replication, or transfer management.
- **No auto-interception of reads.** `build-vs-buy-boundaries.md` says never hook
  `open()`/audit reads at capture time. On-demand resolution is the opposite
  mechanism: an *explicit, agent-initiated, read-only* dereference of a pointer
  the graph already holds. It draws no new edges and writes nothing to the graph.
- No new provenance edges. Resolution reads; it does not commit. (A resolution
  *event* may be logged for audit, like export events, but that is optional.)

## The resolution contract: content hash is the integrity gate

`content_hash` is already designated the cross-tool, cross-machine join key
(`build-vs-buy-boundaries.md`, Pipeline and Lineage Boundary). Resolution makes
that hash earn its keep a second time: every resolve fetches bytes, recomputes
the digest, and compares it to the stored `content_hash`. The result is
tri-state, and the agent must be told which:

- **`verified`** — fetched bytes hash to the stored `content_hash`. This is
  exactly the content the graph reasoned about. Safe to use.
- **`drifted`** — the locator resolved and bytes were inspected, but the digest
  does **not** match. The file moved/changed/was overwritten since capture.
  Surface the diagnostic metadata loudly, but withhold the mismatched bytes;
  never substitute drifted content for the captured artifact.
- **`unresolved`** — no adapter for this `source_system`, the locator is
  unreachable on this host, or access was denied. Return the pointer and the
  reason, not content.

This is what makes "pull content that wasn't captured" trustworthy: the agent
gets new content *and* a guarantee it is the same artifact, or an explicit
warning that it is not.

## Interface

Mirror the existing internal [`FileStorageBackend`](../src/lab_tracker/file_storage.py)
(`store`/`retrieve`/`exists`/`iter_chunks`), but for *outbound* dereferencing.
New module, e.g. `src/lab_tracker/artifact_resolution.py`:

```python
class ResolutionStatus(str, Enum):
    VERIFIED = "verified"
    DRIFTED = "drifted"
    UNRESOLVED = "unresolved"

@dataclass(frozen=True)
class ResolvedArtifact:
    status: ResolutionStatus
    source_system: str
    uri: str
    expected_hash: str
    observed_hash: str | None       # recomputed digest, when bytes were fetched
    content_type: str | None
    size_bytes: int | None
    content: bytes | None           # bounded; present only when verified
    truncated: bool                 # True if size exceeded max_bytes / a range was used
    fetched_at: datetime
    detail: str | None              # reason when unresolved/drifted

class ArtifactResolver(Protocol):
    def can_resolve(self, ref: ExternalArtifactReference) -> bool: ...
    def resolve(
        self,
        ref: ExternalArtifactReference,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        byte_range: tuple[int, int] | None = None,
    ) -> ResolvedArtifact:
        """Fetch a bounded view and verify the content hash."""
```

A `ResolverRegistry` dispatches by `source_system` (with a generic `http`/`https`
fallback), tries each registered adapter's `can_resolve`, and returns
`UNRESOLVED` if none matches. This is the "Adapter configuration" the boundary
doc explicitly permits.

### Adapters, phased

| Adapter | `source_system` | Phase | Notes |
| --- | --- | --- | --- |
| Local filesystem | `local`, or any `file://`/path locator | v1 slice | Resolves a path on *this host*. Needs the host-local mount config below. |
| Generic HTTP(S) | `http`/`https`, `doi` (follow to file) | v1 slice | Bounded, hash-verified reads; public global destinations or explicit exact-authority/CIDR exceptions only. |
| S3 / MinIO | `s3` | next | Object storage; the boundary doc's primary byte target. Credentials via adapter config. |
| OneDrive / Box | `onedrive`, `box` | later | Graph/Box API + OAuth. This is the "offload credential/session/device-grant plumbing later" boundary — do it once auth is offloaded. |
| DataLad / DVC | `datalad`, `dvc` | later | `datalad get` / `dvc pull` by hash; substrate already models content. |

Start with **local + HTTP(S)**, because that covers the scientist's real case:
files consolidated into a OneDrive folder that is **locally synced** on the
machine the agent runs on resolve as local paths today, with no cloud
credentials. The cloud-API adapters are the upgrade for when the file is *not*
synced locally.

## Outbound HTTP destination policy

The generic HTTP(S) adapter treats every persisted URL as untrusted input. A
content hash proves which bytes arrived; it does not make the server-side
connection safe. The adapter therefore authorizes the destination before
sending a request:

1. Parse and normalize the URL. Only HTTP(S) with a valid hostname and port is
   accepted. Malformed URLs, user information, and unsafe literal addresses are
   denied. Local and single-label names require an exact internal exception.
2. Resolve the hostname once and inspect every IPv4 and IPv6 answer. The normal
   public path requires all answers to be globally routable. Private, loopback,
   link-local, unspecified, multicast, reserved, metadata-service, mixed
   public/non-public, or otherwise unsafe answer sets are denied as a whole.
   Link-local metadata-service, unspecified, multicast, reserved, and IPv6
   transition addresses remain forbidden even when an internal exception is
   configured.
3. Connect directly to one of those already-vetted numeric addresses while
   retaining the normalized hostname for the HTTP `Host` header and HTTPS SNI
   and certificate verification. The HTTP client does not consult proxy
   environment variables. This pinning removes the DNS-check/connect race:
   DNS rebinding after authorization cannot substitute a different target.
4. Follow redirects only within a finite hop limit. Each `Location` is parsed,
   authorized, resolved, and pinned independently before another request is
   sent; approval of the initial URL never carries over to a redirect target.
5. Enforce one monotonic wall-clock deadline for the complete resolution. The
   `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS` budget (default: `30`) includes
   DNS, connect and TLS setup, response headers, every redirect hop, body
   verification, and hashing. It is not reset by progress or redirects, so a
   drip-fed response cannot extend the request indefinitely. The setting must
   be finite, greater than zero, and no greater than `86400` seconds (one day);
   invalid values fail application startup. Cancellable lookups use dnspython
   with the host's configured DNS servers and search domains; names available
   only through platform-specific NSS, mDNS, or local-hosts integrations are
   not guaranteed to resolve.

Public global destinations need no operator entry. An internal or otherwise
non-public destination requires both:

- an exact normalized scheme + hostname + effective-port entry in
  `LAB_TRACKER_RESOLVER_HTTP_ALLOWED_AUTHORITIES`; and
- CIDRs in `LAB_TRACKER_RESOLVER_HTTP_ALLOWED_NETWORKS` that contain every
  resolved address, or the literal IP.

The settings are deliberately conjunctive. Neither an authority nor a network
alone grants access, and a single answer outside the configured networks denies
the entire destination. Authority matching is exact after normalization; no
wildcard or hostname-suffix rules are supported. Invalid authority or CIDR
configuration fails application startup rather than silently broadening
access. This exception changes destination reachability only: the pointer stays
read-only, the owning entity's opaque authorization boundary still runs first,
the whole response still passes the content-hash integrity gate, and the fetch
and returned payload remain bounded.

## Admission and database-scope boundary

Resolution is a potentially slow operation even when each resolver has its own
deadline. The HTTP composition is therefore security headers, authentication,
external-artifact admission, the short request database scope, and the route.
Authentication may use its authoritative store; admission never changes the
existing unauthenticated or forbidden response semantics.

After authentication, `POST /external-artifacts/resolve` obtains a
process-local, no-wait lease before an ordinary request session or repository
is constructed. The lease has both a global counter and a counter keyed by
`actor.user_id`. Either exhausted counter produces the same fixed generic
`429` response and `Retry-After` header, without a project, entity, counter, or
resolver detail. A rejected request cannot reach entity lookup, repository
construction, resolver dispatch, network, or subprocess work.

An accepted request first completes opaque entity authorization and all
database-backed preparation, including `store://` materialization. It detaches
the immutable resolution inputs and releases its read scope (rollback/close)
before calling a resolver, so the request's SQLAlchemy connection is returned
to the pool while HTTP, local, rclone, or Git I/O is in progress. The release
is idempotent and the admission lease is released exactly once on normal
completion, resolver failure or timeout, cancellation, disconnect, and other
`BaseException` paths.

The default global/per-actor limits are `8` and `2`, respectively. Both must be
positive integers, the global value is capped at `32` (below the standard
shared AnyIO worker capacity of 40), and the per-actor value cannot exceed the
configured global value. Counters exist only within one process, so the limits
multiply across Uvicorn workers and replicas. The supported deployment uses
one Uvicorn worker per service process; this mechanism is not a cluster-wide
admission system.

## Bounded rclone and Git subprocesses

Rclone and Git resolution use optional host binaries, but a persisted artifact
reference cannot be allowed to control an unbounded child process. The shared
subprocess boundary therefore applies one monotonic deadline to each complete
logical resolution. Rclone's metadata lookup, content transfer, hashing, and
collection consume one budget; Git fetch, object inspection, content transfer,
hashing, and collection consume another. A successful subprocess or incremental
output never resets the deadline.

`LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS` controls that budget
(default: `30`). It is independent from the HTTP deadline and must be finite,
greater than zero, and no greater than `86400` seconds. Metadata stdout and
stderr are captured under separate fixed byte caps. Artifact stdout is streamed
instead: actual bytes read, rather than advisory rclone or Git metadata, are
enforced against the existing `max_fetch_bytes` limit while the full object is
hashed. An object that grows after a size preflight therefore cannot bypass the
fetch cap.

On deadline expiry, output overflow, malformed output, or process failure, the
boundary closes pipes and performs terminate-then-kill-and-reap cleanup under a
separate fixed grace. A failed call can exceed the configured execution
deadline only by this bounded cleanup grace. Resolution then returns a generic
`UNRESOLVED` diagnostic; remote names, paths, credentials, and raw stderr are
never copied into result details. These controls bound execution time and
process-output memory for a resolution. Structural Git remote authorization is
handled separately, and Git fetch disk/cache growth and concurrent cache
containment remain follow-up work.

Persisted Git locators cannot carry HTTP/Git userinfo, URL passwords, or query
strings; those forms are refused before process creation. SSH routing usernames
remain valid, while authentication material must come from host-side Git or SSH
configuration.

### Structural Git remote authorization

Git resolution and Git store-health probes share one immutable
`GitRemotePolicy`, parsed once from `LAB_TRACKER_GIT_ALLOWED_REMOTES` during
application composition. An unset or empty setting creates a deny-all policy.
The configuration is a strict comma-separated list: entries are not trimmed,
and empty, malformed, or semantically duplicate normalized grants abort startup
with a redacted index/category diagnostic.

The accepted grant and candidate forms are deliberately narrower than Git's
full URL grammar:

- `https://host[:port][/path]`
- `ssh://[user@]host[:port][/path]`
- `git://host[:port][/path]`
- `[user@]host:path` and `[user@]host:/path`

The last two forms retain distinct SCP-relative and SCP-absolute semantics.
Parsing case-normalizes and strictly IDNA-canonicalizes DNS names, canonicalizes
numeric IPs and default ports, and then reconstructs the only value that may
enter the Git argument vector. Terminal-dot DNS names are rejected rather than
collapsed into search-eligible names.
Authorization requires an exact scheme, canonical host, effective port, SSH
user, and path style; the grant path must be a case-sensitive, whole-segment
prefix of the candidate path. There are no wildcards or raw string-prefix
matches. Local/drive paths, remote helpers, unsupported or authority-less
schemes, credentials outside an SSH routing username, queries/fragments,
percent escapes, whitespace/control characters, backslashes, malformed paths,
and malformed authorities fail closed before a child can start. Non-root path
segments are restricted to conservative ASCII letters, digits, and `._~+@-`,
with empty, repeated, trailing, dot, and leading-dash segments rejected.

Git itself can transform a remote through configuration. Before any network
query or fetch, the implementation runs a bounded `git ls-remote --get-url`
preflight using the same environment and app-owned working-directory boundary
as the subsequent command. Apart from the required terminal line ending, its
output must be byte-for-byte equal to the reconstructed canonical remote;
structural equivalence alone does not pass. Git HTTP redirects are disabled
both generically and for the approved URL, so neither a rewrite nor a redirect
inherits authorization.

The policy authorizes the logical Git endpoint; it does not independently
constrain every socket opened by Git or SSH. System/global Git configuration is
intentionally retained for credential helpers, HTTP proxy/TLS settings, and SSH
authentication. The SSH agent, keys, and OpenSSH configuration—including
`HostName`, `ProxyJump`, and `ProxyCommand`—and Git/HTTP proxy configuration are
therefore trusted operator state. They can route an approved endpoint through a
different host and must not be writable by an untrusted application user.
Credentials remain outside persisted locators and policy strings. Health probes
additionally run from an app-owned empty non-repository directory, preventing
repository-local configuration from being inherited from the service's launch
checkout. Each Git command also clears inherited repository/object/work-tree
selectors and sets the app-owned operation directory's parent as the
repository-discovery ceiling, so that parent and its ancestors cannot supply
repository-local configuration.

The bounded host-process implementation contains complete descendant trees on
each supported process platform. POSIX hosts use a dedicated process group.
Windows hosts create an unnamed, non-inheritable Job Object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, start the leader suspended, assign and
verify it in the Job Object, and resume its primary thread only after verified
containment. Failure before verified assignment cannot execute artifact
resolver code. Deadline, output-overflow, consumer-failure, and cleanup paths
terminate the whole Job Object; closing its handle is the final kill-on-close
backstop.

## The host-local locator problem

A stored `uri` is portable identity-wise but not location-wise: OneDrive mounts
at a different local path on every machine (`C:\Users\…\OneDrive` vs
`/Users/…/OneDrive`), and a captured `uri` might be a cloud URL, a logical path,
or a machine-specific absolute path. So an adapter needs **host-local config** —
a small mount/endpoint table mapping a `source_system` (and optional URI prefix)
to how it is reachable *here*:

```
[resolvers.onedrive]
local_mount = "/home/sam/OneDrive"     # this host's sync root, if synced
# or
api_base = "https://graph.microsoft.com/v1.0/..."   # later, when not synced
```

The `content_hash` is what makes this safe: even when the path differs across
machines, a successful resolve re-hashes and confirms it found the *same* bytes
(`verified`), so a misconfigured mount surfaces as `drifted`/`unresolved` rather
than silently feeding the agent the wrong file. This is the resolver-side
complement to the cross-machine consolidation described in
[`lab-experiment-documentation.md`](lab-experiment-documentation.md).

### Native local-path authorization

A `file:` URI is converted to a native path before any containment decision.
This matters on Windows: the path component of `file:///C:/Users/...` is
`/C:/Users/...`, which is not itself the native absolute path produced by
`Path.as_uri()`. The URI path is decoded exactly once through the platform's
URL-to-path conversion, so a supported locator produced by `Path.as_uri()`
round-trips to the same native absolute path. The decoder accepts an empty
authority and `localhost` as the sole local authority (ASCII
case-insensitive); UNC/network-share URI forms and every other authority are
denied. Plain native **absolute** paths bypass URI decoding but pass through the
same authorization policy; relative path locators are rejected.

The candidate and each configured `allowed_root` are canonicalized with native
filesystem semantics before comparison. Authorization uses component-aware
containment (`os.path.commonpath` on POSIX and exact canonical components on
Windows), not textual prefix matching, so a sibling whose name merely begins
with an allowed root is denied. Windows drive letters are normalized while
component case remains exact for case-sensitive NTFS directories; cross-drive
comparisons fail closed. Canonicalization rejects static POSIX symlink and
Windows-junction targets outside the configured roots.

Recovery applies the same policy to traversal itself as well as to candidate
files. Every symlink/reparse-point directory is pruned before recursive descent,
and ordinary directory candidates whose canonical targets leave the root are
also removed. The current recovery budget counts candidate files and hashed
bytes; `lab-tracker-n5kp.61` separately owns pre-follow reparse inspection plus
explicit directory-count and wall-clock budgets, so this slice does not
overstate traversal availability.

Canonical pathname authorization is a preliminary plan, not a capability that
the resolver may later reopen. The local access boundary opens one file and
retains that descriptor through hashing and range collection:

- On POSIX, the canonical absolute path is opened one component at a time,
  relative to the preceding directory descriptor. Parent directories and the
  leaf use no-follow operations. An obvious non-regular leaf is rejected by a
  descriptor-relative metadata check before open; the leaf is then opened
  nonblocking and the same fd is revalidated as regular before any content read.
- On Windows, the file is opened once and its borrowed native handle is queried
  with `GetFinalPathNameByHandleW(FILE_NAME_NORMALIZED | VOLUME_NAME_DOS)`.
  Only a supported drive path contained by the canonical roots is accepted, and
  the same CRT descriptor is validated and read. UNC, device, GUID-volume, and
  malformed final namespaces fail closed.

A rename after open cannot redirect the descriptor, and an outside
symlink/junction target is rejected before the first content read. Safe
in-root links remain supported because canonical planning resolves them before
the handle-bound step. This is not snapshot isolation: a concurrent writer can
still produce drift or a mixed sequential view, but the full content-hash gate
prevents unmatched bytes from being returned as `VERIFIED`.

Windows may initiate target-side I/O while opening a path through a reparse
point before the final handle path is authorized. The resolver never returns
those outside bytes; pre-follow reparse inspection and traversal availability
remain explicitly owned by `lab-tracker-n5kp.61`.

### Registered local-store confinement

A registered `local_fs` store is a second authority boundary, not merely a
prefix concatenated onto the broader operator allowlist. Its locator is parsed
without filesystem work into strict relative components, and the raw store root
must already be a supported native absolute path before expansion or
canonicalization. Its name has a portable 1–63-character ASCII grammar, and
legacy rows outside either local contract fail closed. Resolution carries those
values in a frozen prepared target across request-scope release.

The local resolver then requires the complete canonical store root to be
contained by one configured operator root and constructs the exact
handle-bound reader from a short-lived policy rooted at that store. Direct
reads, final-handle validation, and recovery all use this same narrower policy.
There is no fallback to ordinary unscoped local resolution. Consequently a
lexical traversal, static link, raced junction, or content-hash recovery cannot
reach a sibling store even when both stores sit below one broad operator root.
The result retains the canonical logical `store://` URI; concrete host paths are
not exposed in store-scoped diagnostics.

The raw-absolute rule applies to registered store roots, not operator
configuration: global resolver roots preserve their established `~` expansion
and relative-to-process-working-directory behavior. Non-local locator syntax is
also unchanged here; Git, HTTP, and rclone prefix confinement is a separate
adapter-specific follow-up.

## Recovering moved/renamed local artifacts

The `content_hash` is not only the integrity gate — it is a location-independent
identity that survives a rename. So when a local reference's file is *missing at
its `uri`* (the researcher moved or renamed it), the local resolver can recover
it instead of dead-ending at `UNRESOLVED`: it scans the resolver's already
configured `allowed_roots` for a file whose recomputed digest matches the
reference, and returns that file `VERIFIED`. A direct local reference may report
the recovered host path; a registered-store result uses a generic in-store
recovery detail and retains its logical `store://` URI. The stored reference is
**not** rewritten (recovery is read-only — auto-repairing the pointer is
deferred).

Within the canonical-path threat model, recovery preserves these boundaries:

- **Integrity is unchanged.** A recovered file is verified by the same
  re-hash-and-compare as any other resolve, so it is exactly as trustworthy as
  one found at the `uri`; a same-named decoy with different bytes does not match.
- **Handle-bound confinement.** The scan starts from canonical
  `allowed_roots`, prunes linked/reparse directories, and opens each candidate
  through the same handle-bound access layer as direct resolution. Pathname
  replacement cannot redirect the descriptor that is hashed.
- **Opt-in and logically file/byte-bounded.** Recovery is off unless
  `LAB_TRACKER_RESOLVER_RECOVERY` is truthy *and* roots are configured. A
  `RecoveryPolicy` budget (`max_files`, `max_bytes`, overridable via
  `LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES` / `_MAX_BYTES`) caps the scan;
  candidates that share the original basename are tried first so the common case
  (a rename that kept the filename, or a moved parent directory) is cheap. When
  the same-fd size hint fits, the remaining logical hashing budget is still
  debited as chunks are read—even when a later read fails—so failed or growing
  candidates cannot collectively exceed it. Exact OS bytes-read and local
  resolution deadlines are tracked by
  `lab-tracker-n5kp.47`; directory/time traversal bounds are tracked by
  `lab-tracker-n5kp.61`.

## Read-surface integration

Resolution is an **explicit follow-up call**, not something that bloats every
context response. The decision-context payload (`POST /assistant/decision-context`,
`mcp_tools/read.py` → `lab_tracker_get_decision_context`) and the provenance
documents (`routes/provenance.py`, `provenance.py:_external_artifact_node`)
already hand the agent the pointer. The agent decides *when* it needs bytes and
makes one more call:

- **HTTP (read-only):** `POST /external-artifacts/resolve` with
  `entity_type` (`dataset`, `analysis`, or `claim`), `entity_id`, and an optional
  `artifact_index` (default `0`). Optional `content_hash`, `max_bytes`,
  `byte_start`, and `byte_end` fields constrain and verify the selected
  reference; byte-range bounds must be supplied together. The response contains
  the `ResolvedArtifact` fields plus the selected entity type and ID, artifact
  index, and base64-encoded content.
- **MCP read tool:** `lab_tracker_resolve_artifact(entity_type, entity_id,
  artifact_index=0, content_hash?, max_bytes?, byte_start?, byte_end?)`, sitting
  next to `lab_tracker_get_dataset_provenance` / `_analysis_provenance` /
  `_claim_provenance` in `mcp_tools/read.py`. Stays within "read-only assistant
  and MCP decision-context endpoints" — no autonomous graph commits.

Authorization is resolve-by-entity and uses the **same opaque project-read
boundary** as reading the dataset, analysis, or claim that embeds the reference.
The entity must be found and authorized before artifact-index selection,
caller-supplied content-hash comparison, data-store materialization, or resolver
work. A missing entity and an existing but inaccessible entity therefore return
the same canonical entity-not-found response regardless of whether the supplied
index or hash would be valid. The resolver cannot become a way to enumerate
artifacts or fetch bytes for a project the caller cannot read.

## Bounding and untrusted content

Resolving arbitrary external content into an agent's context is a payload-size
and prompt-injection surface, so the resolver is bounded by construction:

- **Size cap.** `max_bytes` bounds the bytes returned in `content_base64`; the
  whole artifact is still hashed for integrity and `truncated=True` reports
  that the response omits bytes. Optional `byte_start`/`byte_end` fields request
  a bounded slice and must be supplied together.
- **Integrity before content.** Only `verified` results include
  `content_base64`. `drifted` results preserve the observed hash, full size,
  content type, truncation flag, and mismatch detail while returning
  `content_base64=null` and `returned_bytes=0`.
- **Content type is metadata.** The response reports `content_type`, but both
  text and binary payloads use bounded base64 in the current HTTP/MCP contract.
  There is no implemented metadata-only HEAD endpoint or streaming handle.
- **HTTP display locators are redacted.** An unresolved HTTP result reports a
  generic redacted locator. Verified or drifted HTTP results omit URL user
  information, query, and fragment components, so signed-URL credentials and
  denied targets are not copied into API/MCP response envelopes or errors.
- **Untrusted by default.** Resolved bytes are external data and must be tagged
  as such where surfaced (the same caution the project already applies to
  webhook/PR content), so downstream reasoning treats file contents as data, not
  instructions.
- **Optional content-addressed cache.** Resolved bytes may be cached through the
  existing `FileStorageBackend` keyed by `content_hash` (a read-through cache),
  so repeated resolves don't re-fetch. Keep it a dumb content-addressed cache —
  not lifecycle/replication management, which the anti-scope guardrails exclude.

## Why this respects the boundary

- **Pointer model preserved.** The graph still stores references, not bytes;
  resolution is a separate, on-demand read path.
- **Adapters stay thin and optional.** Each `source_system` adapter only knows
  how to fetch and is added when needed; the registry degrades to `UNRESOLVED`.
- **Hash stays the join key.** The same digest that links artifacts across
  machines now also certifies that a resolved view is the captured artifact.
- **Read-only, no interception, no new edges.** It extends the assistant/MCP read
  surface and draws nothing into the graph.

## Implementation status

Shipped (`src/lab_tracker/artifact_resolution.py`, tested in
`tests/test_artifact_resolution.py` and `tests/test_external_artifacts_routes.py`):

- ✅ `ResolutionStatus` (`verified`/`drifted`/`unresolved`), the frozen
  `ResolvedArtifact` result with `to_json_dict()`, the `ArtifactResolver`
  protocol, and a `ResolverRegistry` that dispatches to the first capable
  adapter and falls back to `UNRESOLVED`.
- ✅ `LocalFilesystemResolver` — `file://` and `local`/`local_fs` sources, with
  native `file:` URI conversion, an empty/`localhost`-only authority policy,
  and canonical `allowed_roots` containment. Component-aware comparison uses
  `commonpath` on POSIX and exact canonical components on Windows to deny
  sibling-prefix, case-sensitive-sibling, and cross-drive escapes; static
  symlink and Windows-junction targets outside the roots are rejected. A narrow
  handle-bound reader then validates a same-descriptor regular file: POSIX uses
  descriptor-relative no-follow component traversal, while Windows validates
  the normalized final path from the borrowed handle before reading. Hashing
  never reopens an authorized pathname.
- ✅ Content-hash recovery of moved/renamed local artifacts — opt-in
  (`LAB_TRACKER_RESOLVER_RECOVERY`), logically file/byte-bounded by a
  `RecoveryPolicy`,
  scans only `allowed_roots`, prunes symlink/junction escapes, and searches
  basename-first. Each candidate's same descriptor supplies the size hint and
  bytes being hashed; a missing file whose bytes still exist under a root
  resolves `VERIFIED` instead of `UNRESOLVED` (read-only; the `uri` is not
  rewritten).
- ✅ Registered `local_fs` targets carry their validated locator and trusted
  store root through database-scope release. The exact handle-bound reader and
  recovery roots are narrowed to that store root while remaining conjunctive
  with the operator allowlist; invalid or mismatched logical store locators fail
  closed before candidate filesystem work.
- ✅ `HttpResolver` — `http(s)`, full-body verify with a `max_fetch_bytes` cap
  (oversized → `UNRESOLVED`, never uncertified bytes), plus a shared outbound
  destination policy that validates every IPv4/IPv6 answer, pins the vetted
  connection address, ignores proxy environment variables, and reauthorizes
  each bounded redirect hop. Public destinations must resolve entirely to
  globally routable addresses; internal exceptions require the exact-authority
  and CIDR settings described above.
- ✅ `RcloneResolver` — `rclone://<remote>/<path>`, the locked-in unifier for
  S3 / SFTP / Dropbox / Google Drive / Box / OneDrive; stats then fetches, and
  degrades to `UNRESOLVED` when the binary is absent. Gated by an operator
  remote-name allowlist (`LAB_TRACKER_RCLONE_ALLOWED_REMOTES`, deny-by-default
  when unset) so a reference cannot drive server-side `rclone cat` against
  arbitrary remotes in the host's rclone config — the same opt-in posture as
  local allowed roots and the git remote allowlist. Metadata and stderr are
  independently capped, content is streamed under the actual-byte fetch limit,
  and one subprocess deadline covers stat, transfer, and verification; failed
  process cleanup uses the separate fixed grace described above.
- ✅ `GitResolver` — resolves a pinned repository object only after the remote
  allowlist check, with one subprocess deadline across fetch, object metadata,
  streamed content verification, and bounded cleanup. Metadata and stderr are
  independently capped, while the object stream is subject to the actual-byte
  fetch limit. Structural remote policy and cache disk containment remain
  separate hardening work.
- ✅ Content hash is the integrity gate across all adapters (the whole object is
  hashed; `max_bytes`/`byte_range` bound only the returned payload), via the
  shared `_hash_and_collect` helper.
- ✅ `POST /external-artifacts/resolve` — resolve-by-entity, gated by the owning
  dataset, analysis, or claim's opaque read boundary before artifact selection,
  hash comparison, materialization, or resolver work; authenticated requests
  are then admitted under process-local global and per-actor no-wait limits.
  Saturated requests return the same generic `429` plus `Retry-After` without
  constructing the ordinary request session. Accepted calls complete all
  database-backed preparation and release their read scope before resolver I/O;
  returns the envelope plus base64 content. Registry comes from
  `request.app.state.resolver_registry` or
  `registry_from_env()`; `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS` gates local roots
  (unset → local artifacts resolve `UNRESOLVED`), HTTP(S) is constrained by the
  outbound destination policy, and rclone is constrained by its configured
  remote-name allowlist. HTTP resolution additionally uses the single total
  deadline configured by `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS`; rclone
  and Git use the independent single total deadline configured by
  `LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS`.
- ✅ `lab_tracker_resolve_artifact` MCP read tool + `resolve_external_artifact`
  client method.

Deferred (depend on the data store registry or new dependencies):

- ⏭️ Native `s3` (boto3) and `ssh` (paramiko) adapters — currently covered by
  rclone; native versions add dependencies for mostly-redundant coverage, and
  native S3 `versionId` snapshots belong with the registry's
  `versioned_snapshot` capability. See
  [`data-store-registry-design.md`](data-store-registry-design.md).
- ⏭️ `database` adapter (`query → rows`, snapshot/`unversioned` semantics) —
  needs the registry's connection config; do not smuggle a connection string
  through the reference `uri`.
- ⏭️ A `HEAD`-style metadata-only variant and the optional content-addressed
  cache via `FileStorageBackend`.

## See also

- [`build-vs-buy-boundaries.md`](build-vs-buy-boundaries.md) — byte/asset
  storage boundary, the content-hash join key, and the anti-scope guardrails.
- [`lab-experiment-documentation.md`](lab-experiment-documentation.md) and
  [`experiment-walkthrough-coverage.md`](experiment-walkthrough-coverage.md) —
  the multi-machine → single-store consolidation that motivates resolution.
- [`mcp-decision-context-tooling.md`](mcp-decision-context-tooling.md) — the
  read-only assistant surface this extends.
