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

## Recovering moved/renamed local artifacts

The `content_hash` is not only the integrity gate — it is a location-independent
identity that survives a rename. So when a local reference's file is *missing at
its `uri`* (the researcher moved or renamed it), the local resolver can recover
it instead of dead-ending at `UNRESOLVED`: it scans the resolver's already
configured `allowed_roots` for a file whose recomputed digest matches the
reference, and returns that file `VERIFIED`. The recovery detail records the path
it was found at; the stored `uri` is **not** rewritten (recovery is read-only —
auto-repairing the pointer is deferred).

This never widens the trust or security surface:

- **Integrity is unchanged.** A recovered file is verified by the same
  re-hash-and-compare as any other resolve, so it is exactly as trustworthy as
  one found at the `uri`; a same-named decoy with different bytes does not match.
- **Never reads outside the allowed roots.** The scan walks only `allowed_roots`
  and re-checks path containment (after symlink resolution) per candidate, so it
  cannot become an oracle for probing arbitrary host files by hash.
- **Opt-in and bounded.** Recovery is off unless
  `LAB_TRACKER_RESOLVER_RECOVERY` is truthy *and* roots are configured. A
  `RecoveryPolicy` budget (`max_files`, `max_bytes`, overridable via
  `LAB_TRACKER_RESOLVER_RECOVERY_MAX_FILES` / `_MAX_BYTES`) caps the scan;
  candidates that share the original basename are tried first so the common case
  (a rename that kept the filename, or a moved parent directory) is cheap. When
  nothing matches within budget the result is `UNRESOLVED` — identical to today.

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
  `allowed_roots` path-containment so resolution cannot read arbitrary host files.
- ✅ Content-hash recovery of moved/renamed local artifacts — opt-in
  (`LAB_TRACKER_RESOLVER_RECOVERY`), bounded by a `RecoveryPolicy`, scans only
  `allowed_roots`, basename-first; a missing file whose bytes still exist under a
  root resolves `VERIFIED` instead of `UNRESOLVED` (read-only; the `uri` is not
  rewritten).
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
  local allowed roots and the git remote allowlist.
- ✅ Content hash is the integrity gate across all adapters (the whole object is
  hashed; `max_bytes`/`byte_range` bound only the returned payload), via the
  shared `_hash_and_collect` helper.
- ✅ `POST /external-artifacts/resolve` — resolve-by-entity, gated by the owning
  dataset, analysis, or claim's opaque read boundary before artifact selection,
  hash comparison, materialization, or resolver work; returns the envelope plus
  base64 content. Registry comes from `request.app.state.resolver_registry` or
  `registry_from_env()`; `LAB_TRACKER_RESOLVER_ALLOWED_ROOTS` gates local roots
  (unset → local artifacts resolve `UNRESOLVED`), HTTP(S) is constrained by the
  outbound destination policy, and rclone is constrained by its configured
  remote-name allowlist. HTTP resolution additionally uses the single total
  deadline configured by `LAB_TRACKER_RESOLVER_HTTP_DEADLINE_SECONDS`.
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
