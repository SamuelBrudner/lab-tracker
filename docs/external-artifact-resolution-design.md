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
    truncated: bool                 # bounded selected view is smaller than full artifact
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

## Bounded resolver and store-health subprocesses

Local resolution uses a fixed isolated Python helper, while rclone and Git use
optional host binaries; a persisted artifact reference cannot be allowed to
control any of them without bounds. The shared subprocess boundary therefore
applies one monotonic deadline to each complete logical resolution. A local
direct attempt and all recovery candidates consume one budget; rclone's metadata
lookup, content transfer, hashing, and collection consume another; Git fetch,
object inspection, content transfer, hashing, and collection consume another.
A successful subprocess, recovery attempt, or incremental output never resets
the deadline.

`LAB_TRACKER_RESOLVER_SUBPROCESS_DEADLINE_SECONDS` controls that budget
(default: `30`). It is independent from the HTTP deadline and must be finite,
greater than zero, and no greater than `86400` seconds. It also gives each
local, rclone, or Git store-health probe a fresh bounded execution budget; local
health creates that budget before its filesystem-I/O-free lexical admission and
passes the exact deadline through its bounded filesystem broker. Metadata stdout
and stderr are captured under separate fixed byte caps. Local artifact stdout
is streamed under the cumulative local-read allowance (plus only the fatal EOF
proof byte); rclone and Git artifact stdout is streamed under `max_fetch_bytes`.
Every adapter still hashes the full object even when the returned view is
smaller.

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

Application composition parses the configured roots once into a
filesystem-I/O-free `LocalFilesystemAuthority`. Pure component comparison
selects the most-specific lexical grant, so a malformed, disjoint, cross-drive,
or sibling-prefix candidate is denied before a helper starts or the target
filesystem is touched. Direct resolution passes the decoded native path and
that one selected grant to the shared bounded local-filesystem broker. It does
not canonicalize, stat, or open the candidate in the application process.

The normative mount and namespace contract is
[`configuration.md`](configuration.md#mount-and-namespace-authority). In
particular, an allowed root grants the transitive subtree visible in the
service namespace: POSIX ordinary and bind mounts beneath it remain authorized,
while unsupported Windows nested volume mounts and UNC/device/GUID namespaces
fail closed. This authority is about namespace reachability, not device
identity. Symlinks and junctions are aliases rather than mount grants and are
eligible only when bounded resolution proves their destination remains inside
the same root. Non-name-surrogate Cloud Files directory placeholders are not
mount crossings.

The broker runs one fixed, isolated, stdlib-only helper through the shared
bounded process executor. The helper first resolves and retains the selected
operator grant, then traverses the candidate component-by-component from
no-follow descriptors or handles. POSIX aliases are read and rewritten from
their exact directory entry; Windows symlink and junction reparse data is read
from the exact retained handle. An escape or unsupported Windows namespace is
denied before its target is traversed. The regular-file leaf remains open on
that retained descriptor or handle while its complete bytes are streamed to the
application's hash/view collector; no canonical pathname plan crosses the
process boundary and no accepted object is reopened by path.

A rename after open cannot redirect the descriptor, and an outside
symlink/junction target is rejected before it is followed. Before/after metadata
snapshots, an exact-size read, and one EOF proof detect many concurrent changes,
but this is not snapshot isolation and does not promise to detect every
same-size rewrite whose compared metadata is unchanged. The full content-hash
gate prevents bytes with a different digest from being returned as `VERIFIED`.

One `LocalResolutionBudget` owns the complete logical operation: its default and
hard maximum is 512 MiB of accepted full-file payload, and its one absolute
subprocess deadline defaults to 30 seconds. A direct attempt and every recovery
candidate share that same budget. The helper reads exactly the size in its first
metadata snapshot and then makes one one-byte EOF read. The raw process-output
ceiling is therefore the remaining allowance plus one solely to expose a fatal
growth proof; that proof byte is never accepted. A stable file exactly equal to
the remaining allowance succeeds when the EOF read is empty. Clean zero-output
missing/denied attempts release their reservation, while any partial, malformed,
timed-out, changed, or otherwise ambiguous attempt consumes the remainder and
makes recovery terminal. All such failures are reported with static,
path-free details.

Recovery enumeration and root metadata are the remaining transitional
application-side filesystem work. `os.walk` still produces candidate names and
prunes directories with the legacy path policy, but every candidate's
authorization/open/read uses the broker and the same budget. Deadline checks
run between enumeration steps; an individual blocked directory call is not yet
interruptible. Pre-follow-safe bounded enumeration, an explicit directory
limit, and precise Cloud Files classification remain
`lab-tracker-n5kp.61`.

### Registered local-store confinement

A registered `local_fs` store is a second authority boundary, not merely a
prefix concatenated onto the broader operator allowlist. Its locator is parsed
without filesystem work into strict relative components, and the raw store root
must already be a supported native absolute path before expansion or
canonicalization. Its name has a portable 1–63-character ASCII grammar, and
legacy rows outside either local contract fail closed. Resolution carries those
values in a frozen prepared target across request-scope release.

The broker first admits the raw store root against one configured operator
grant. Inside the helper it resolves and retains that grant, traverses and
retains the registered store root, and makes the resulting directory stack a
second nested boundary before it traverses the locator. Direct and recovery
candidate reads use this retained nested-store scope; there is no fallback to
ordinary unscoped local resolution. Consequently traversal, aliases, races, and
content-hash recovery cannot reach a sibling store even when both stores sit
below one broad operator root. Recovery enumeration is still narrowed with a
transitional store-root policy, but candidate bytes cross only the nested helper
role. The result retains the canonical logical `store://` URI; concrete host
paths are not exposed in store-scoped diagnostics.

The raw-absolute rule applies to registered store roots, not operator
configuration: global resolver roots preserve current-user `~` expansion from
the process environment and relative-to-process-working-directory behavior.
Named-user tilde forms are rejected without account lookup. Non-local locator
syntax is also unchanged here; each remote adapter owns a separate typed
authority boundary.

### Bounded advisory local-store health

Application composition parses `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS` once using
the host's `os.pathsep` (`:` on POSIX, `;` on Windows). Unset, empty, and
whitespace-only configuration creates a deny-all runtime authority. The
filesystem-I/O-free authority preserves unambiguous lexical components, expands
the current-user `~` from the process environment, prefixes relative roots with
the startup working directory, and rejects dot/dot-dot or unsupported platform
spellings rather than canonicalizing them.
One bounded filesystem broker serves local health, direct local reads, and every
recovery candidate read. A transitional `LocalPathPolicy` derived from the same
roots remains only for recovery enumeration and root metadata until
`lab-tracker-n5kp.61`. Health admits only a configured lexical root spelling;
the separate physical spelling behind an operator root alias must be configured
explicitly if registered stores use it.

Local-store health is an explicit read-only advisory probe, never a side effect
of registration. It creates one absolute deadline before invoking the broker.
Pure component comparison selects the most-specific containing grant; deny-all,
malformed, disjoint, and sibling-prefix candidates return only the static
failure and perform no target filesystem or process work. The broker passes the
lexically admitted candidate and selected root in one compact, versioned,
bounded ASCII
JSON environment value to a fixed absolute sibling stdlib-only helper launched
as `sys.executable -I -S -B <helper>`. Paths never appear in argv or output.

Inside the deadline, the helper resolves the trusted operator root and retains
its handle. It walks only one candidate component at a time relative to retained
handles. POSIX reads symlink text no-follow and rewrites relative or absolute
in-grant targets in a bounded state machine before opening target components.
Windows reads and validates exact-handle symlink/junction reparse data before
rewriting an in-grant target; nested volume-GUID mounts, unsupported
UNC/device/GUID targets, malformed payloads, and escapes are denied before
target traversal. Eligible D-bit/N-clear Cloud directories remain traversable.
No canonical pathname plan crosses the process boundary, and no accepted object
is closed and reopened by path.

The shared bounded executor contains the helper's descendants. The exact
deadline covers broker admission, protocol construction, interpreter startup,
trusted-root anchoring, alias resolution, opens and validation, and process
drain, then is checked again after the executor returns. Terminate/kill/reap has
its separate fixed cleanup grace. Only the accessible exit with zero output is
healthy; denial, timeout, containment failure, unknown/nonzero exit, output, and
ordinary adapter errors collapse to the same static detail. Adapter-level
`BaseException` still propagates after executor-owned cleanup. The result remains
an advisory point-in-time snapshot rather than a durable lease. Mount crossings
follow the normative namespace-transitive policy in
[`configuration.md`](configuration.md#mount-and-namespace-authority).

## Recovering moved/renamed local artifacts

The `content_hash` is not only the integrity gate — it is a location-independent
identity that survives a rename. So when a local reference's file is *missing at
its `uri`* (the researcher moved or renamed it), the local resolver can recover
it instead of dead-ending at `UNRESOLVED`: it scans the resolver's already
configured `allowed_roots` for a file whose recomputed digest matches the
reference, and returns that file `VERIFIED`. Direct and registered-store results
both use static path-free recovery details and retain the original reference
URI; a registered result therefore keeps its logical `store://` identity. The
stored reference is **not** rewritten (recovery is read-only — auto-repairing
the pointer is deferred).

Recovery preserves these boundaries:

- **Integrity is unchanged.** A recovered file is verified by the same
  re-hash-and-compare as any other resolve, so it is exactly as trustworthy as
  one found at the `uri`; a same-named decoy with different bytes does not match.
- **Retained-handle confinement.** Enumeration starts from the configured roots
  and prunes linked/reparse directories with the transitional path policy. Each
  yielded candidate is independently authorized, opened, and streamed by the
  same pre-follow-safe helper as the direct read. A registered-store candidate
  additionally stays beneath the helper-retained nested store boundary.
  Pathname replacement cannot redirect the descriptor or handle that is hashed.
- **Opt-in and file/byte/deadline-bounded where implemented.** Recovery is off
  unless `LAB_TRACKER_RESOLVER_RECOVERY` is enabled *and* roots are configured.
  The typed runtime defaults to at most `4096` candidate files and one cumulative
  `536870912`-byte (512 MiB) direct-plus-recovery read allowance, with the latter
  also the hard maximum. Candidates sharing the original basename are tried
  first. Every successful complete candidate read debits its exact full payload;
  ambiguous reads terminate the logical budget rather than continuing after
  uncertain consumption. The request's separate `max_bytes` remains only the
  returned-view cap (8 MiB hard/default), never a substitute for hashing the
  whole file.
- **Enumeration caveat.** The one local subprocess deadline is reused and
  checked between scan and read steps, but the transitional application-side
  `os.walk` has no directory-count limit and cannot interrupt a single blocked
  directory call. Moving root metadata and enumeration into a bounded broker
  role remains `lab-tracker-n5kp.61`.

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
  reference. `max_bytes` is an exact integer from `1` through 8 MiB; byte-range
  bounds are exact integers from `0` through `2**53 - 1`, must be supplied
  together, and use an exclusive end greater than or equal to the start. The
  response contains the `ResolvedArtifact` fields plus the selected entity type
  and ID, artifact index, and base64-encoded content.
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

- **One hard inline cap.** `max_bytes` defaults to, and cannot exceed,
  `8 * 1024 * 1024` decoded content bytes. Booleans, coercible strings/floats,
  zero, negatives, and larger values are invalid. Optional
  `byte_start`/`byte_end` fields request `[start, end)` and must be supplied
  together as exact, non-negative integers no greater than `2**53 - 1`, with
  `end >= start`. These scalar bounds are validated before entity lookup or
  resolver work. Only HTTP/MCP/application request boundaries interpret an
  omitted value as the default; direct registry, resolver, and collector calls
  require the selected limit as an exact integer and reject explicit `None`.
- **Ranges never enlarge the response.** A ranged request retains only
  `[start, min(end, start + max_bytes))` while streaming. It does not collect
  the requested range and truncate afterward. The whole artifact is still
  hashed for integrity and the adapters' independent fetch ceilings still
  apply.
- **Truthful view metadata.** `returned_bytes` counts decoded raw bytes and
  `size_bytes` remains the full artifact size. `truncated` means the bounded
  selected view is smaller than the full artifact, before integrity withholding;
  merely supplying a full-covering range does not make it true. Ranged content
  is the earliest capped prefix of the requested range. Base64 and JSON add
  transport overhead but cannot increase the decoded-content allowance.
- **Defense in depth.** Concrete resolvers validate before I/O. The registry and
  application serialization boundary share one postcondition. It snapshots the
  prepared logical identity before untrusted dispatch, anchors source, URI, and
  expected hash to that snapshot, validates safe field types, integrity state,
  full size, selected-view length, and truncation, and rechecks the effective
  allowance. Accepted output is copied into a detached result before
  serialization, so later adapter mutation cannot change it. A custom adapter
  that violates any part fails closed to a content-free, redacted `unresolved`
  result; its fields are never serialized or truncated and returned.
  Precomputed prepared results are permitted only for the application's exact
  content-free, redacted store failures.
- **Adapter integrity responsibility.** Registered resolver implementations are
  trusted application adapters and remain responsible for recomputing the full
  artifact digest. The shared postcondition independently rehashes any complete,
  untruncated returned artifact. It cannot reconstruct a full digest from a
  bounded ranged/truncated view, so those results are accepted only after the
  adapter reports the requested identity and matching observed hash; built-in
  adapters obtain that value from the full stream, not from the returned prefix.
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
  and a shared filesystem-I/O-free lexical authority plus bounded broker.
  Component-aware admission denies malformed, sibling-prefix, and cross-drive
  candidates without target I/O. The isolated helper then anchors the selected
  grant and follows POSIX aliases or Windows symlink/junction reparse data one
  component at a time from retained no-follow descriptors/handles. Escapes and
  unsupported namespaces fail before target traversal; the retained regular-file
  object is streamed without reopening an authorized pathname.
- ✅ Content-hash recovery of moved/renamed local artifacts — opt-in
  (`LAB_TRACKER_RESOLVER_RECOVERY`), basename-first and candidate-file-bounded by
  a typed `RecoveryPolicy`. One logical budget shares a 512 MiB default/hard-max
  cumulative full-read allowance and one subprocess deadline across the direct
  attempt and all candidates. A stable exact-limit file succeeds after an empty
  EOF proof; the broker's allowance-plus-one raw ceiling exists only so a
  nonempty proof fails terminally. Clean zero-output misses/denials release a
  reservation, while ambiguous outcomes terminate the budget. Recovery is
  read-only, preserves the original URI, and emits path-free details.
- ✅ Registered `local_fs` targets carry their validated locator and trusted
  store root through database-scope release. The helper anchors the selected
  operator grant and then the exact registered root as a retained nested scope
  before traversing the locator. Direct and recovery candidate reads remain
  conjunctive with both boundaries; invalid or mismatched logical locators fail
  closed before candidate filesystem work.
- ✅ Local-store health and local artifact reads consume narrow roles on the
  exact same bounded operations broker, authority, and process executor.
  Directory inspection has zero-byte output caps; file reads stream only opaque
  bytes under the logical reservation. Results and ordinary failures stay
  static, redacted, and point-in-time rather than durable filesystem leases.
- ⚠️ Recovery root metadata and enumeration remain transitional
  application-side operations. They reuse the deadline and produce candidates
  only; candidate bytes always cross the broker. A blocked directory call is not
  yet interruptible, and a directory-count/pre-follow-safe enumeration role
  remains `lab-tracker-n5kp.61`.
- ✅ `HttpResolver` — `http(s)`, full-body verify with a `max_fetch_bytes` cap
  (oversized → `UNRESOLVED`, never uncertified bytes), plus a shared outbound
  destination policy that validates every IPv4/IPv6 answer, pins the vetted
  connection address, ignores proxy environment variables, and reauthorizes
  each bounded redirect hop. Public destinations must resolve entirely to
  globally routable addresses; internal exceptions require the exact-authority
  and CIDR settings described above.
- ✅ Registered HTTP stores use a separate, nominally dispatched typed target.
  Its base URL and locator are parsed into canonical origin/path components
  before DNS, composed exactly once, and checked against the registered prefix
  before every redirect hop. Invalid legacy definitions and escaping redirects
  fail with the opaque store result before the next network operation, while
  successful results retain their logical `store://` identity. Ordinary direct
  HTTP references keep their existing redirect behavior.
- ✅ `RcloneResolver` — `rclone://<remote>/<path>`, the locked-in unifier for
  S3 / SFTP / Dropbox / Google Drive / Box / OneDrive; stats then fetches, and
  degrades to `UNRESOLVED` when the binary is absent. Gated by an operator
  immutable exact remote-name policy (`LAB_TRACKER_RCLONE_ALLOWED_REMOTES`,
  deny-by-default when unset) so a reference cannot drive server-side
  `rclone cat` against
  arbitrary remotes in the host's rclone config — the same opt-in posture as
  local allowed roots and the Git remote policy. The direct resolver default is
  also deny-all. Metadata and stderr are independently capped, content is
  streamed under the actual-byte fetch limit, and one subprocess deadline
  covers stat, transfer, and verification; failed process cleanup uses the
  separate fixed grace described above.
- ✅ Registered rclone stores use a separate nominally dispatched target.
  `RcloneRemoteName` preserves one exact configured remote, while
  `RegisteredRcloneRoot` retains `remote:path` versus `remote:/path` as
  structural state. Preparation validates the portable locator and total
  root-plus-locator budget before process work, then a frozen factory-only
  `RcloneStoreResolutionTarget` crosses the database-scope boundary. The scoped
  resolver checks the typed remote directly against the allowlist and composes
  one exact positional token without URI decoding or path normalization.
  Results retain the logical `store://` identity. Ordinary direct
  `rclone://` references keep their existing parser and subprocess lifecycle.
- ✅ Rclone and Git store-health subprocesses use dedicated adapters over the
  same object-identical immutable policies and bounded process executor as
  resolution. Rclone preserves relative, rooted, and sole-root registered
  targets and runs one fixed bounded `lsf`; Git preserves its canonical URL
  preflight, redirect denial, sanitized environment, app-owned working
  directory, and one deadline across preflight plus `ls-remote HEAD`. Ordinary
  failures return one static redacted detail per adapter, while adapter-level
  `BaseException` propagates after executor-owned cleanup.
- ✅ `GitResolver` — resolves a pinned repository object only after the remote
  allowlist check, with one subprocess deadline across fetch, object metadata,
  streamed content verification, and bounded cleanup. Metadata and stderr are
  independently capped, while the object stream is subject to the actual-byte
  fetch limit. Structural remote policy and cache disk containment remain
  separate hardening work.
- ✅ Registered Git stores use a separate nominally dispatched target.
  `PinnedGitPath` combines a strict portable repository path with a full
  lowercase nonzero SHA-1 or SHA-256 object ID; mutable refs, abbreviations,
  revspecs, traversal, and platform-specific path aliases fail before cache or
  process work. Preparation retains a neutral structurally parsed remote and
  logical `store://` identity. The scoped resolver reauthorizes that address,
  uses object-format-separated cache namespaces, and explicitly runs
  `git init --object-format=sha1|sha256` before the existing exact-URL preflight,
  fetch, size, stream, and hash lifecycle. Ordinary direct `git+` references
  retain their established parsing and cache behavior pending separate generic
  Git hardening.
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
  `registry_from_env()`; `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS` is parsed with the
  host's `os.pathsep` and gates local roots (unset or empty → local artifacts
  resolve `UNRESOLVED` and local health fails closed), HTTP(S) is constrained by
  the outbound destination policy, and rclone is constrained by its configured
  remote-name allowlist. HTTP resolution additionally uses the single total
  deadline configured by `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS`; local,
  rclone, and Git resolution plus local, rclone, and Git health use the
  independent deadline configured by
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
