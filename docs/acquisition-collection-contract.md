# Acquisition Collection Contract

## Backend-foundation scope

This contract is deliberately limited to the backend foundation: domain and
wire models, relational persistence and migrations, repository/service
commands, HTTP routes, authorization/read-opacity behavior, schema discovery,
and concurrency and integrity rules. It makes Experiment and acquisition
collection records safe to create and query, but does not claim end-to-end
product integration.

An acquisition run can produce thousands of files without producing thousands
of Lab Tracker work entities. Lab Tracker represents that output as one logical
collection attached to a Session and a sequence of content-addressed snapshots.
The collection members are data-level facts inside an immutable manifest; they
are not SQL rows, ordinary project-graph nodes, or default provenance nodes.

## Manifest v1

The wire and stored manifest shape is:

```json
{
  "schema_version": 1,
  "members": [
    {
      "path": "trial-0001/data.bin",
      "checksum": "lowercase SHA-256",
      "size_bytes": 123
    }
  ]
}
```

Members are sorted by path before hashing. Paths must already be normalized
relative POSIX paths: absolute paths, backslashes, blank segments, `.` and `..`
segments, NULs, duplicates, and surrounding whitespace are rejected. Checksums
are exactly 64 lowercase hexadecimal SHA-256 characters. Sizes are nonnegative
signed 64-bit integers.

The canonical encoding is compact, key-sorted UTF-8 JSON with `ensure_ascii`
disabled. `manifest_hash` is SHA-256 over those bytes and therefore includes
the schema version, member paths, checksums, and sizes. It excludes the
collection key, source URI/provider, observation time, capture principal, and
completeness state. Reordering filesystem enumeration cannot change identity;
changing any member fact does.

Manifest v1 is bounded to 10,000 members and 16 MiB of canonical JSON. Larger
acquisitions must use multiple named collections until a later chunked manifest
contract is introduced.

Servers advertise this wire contract as `acquisition_collections_v1` in the
top-level `capabilities` list returned by `GET /schema/describe`.

## Snapshot and lifecycle rules

A logical collection is unique by `(session_id, collection_key)`, and the key is
immutable. Snapshots hold immutable manifest content. Completeness is a
monotonic seal from incomplete to complete; changing members creates a new
snapshot.

Capture retries are idempotent by client capture ID and content hash. An older
offline observation may remain in history but cannot replace a newer current
snapshot. Two different manifests observed at the same timestamp are a
conflict rather than an arbitrary ordering choice.

Content identity and capture observations are stored separately. Every capture
receipt retains its client capture ID, observation time, reported completeness,
source URI/provider, and acting-principal provenance. An unchanged rescan
therefore reuses the immutable manifest snapshot while advancing the
collection's current observation pointer. Compact collection reads use that
selected observation's source, time, capture ID, and principal facts; the
snapshot's completeness remains a monotonic seal and cannot be reversed by a
later incomplete report.

Lab Tracker keeps a durable database copy of canonical manifest JSON, loaded
only by manifest/member endpoints. Summary responses contain only IDs, hashes,
counts, bytes, source facts, and observation time.

## Experiment identity

`Experiment` is a lightweight semantic grouping with one required primary
Question and independent many-to-many Session and Dataset memberships. It does
not model trials, protocols, conditions, samples, wells, instruments, or
metrics. Membership links remain outside Dataset content identity.

## Deferred integrations

The following integrations are intentionally outside this backend-foundation
slice:

- Dataset snapshot-reference fields, collection-aware Dataset hashes, and
  Session-collection-to-Dataset promotion;
- record export, evidence bundle, ARA artifact, and other export surfaces;
- ownership reassignment, offboarding, and durable service/system principal
  instance profiles (service/system captures retain their principal type but
  do not mislabel the linked user as the principal instance);
- MCP tools/resources and generated client bindings;
- decision-context, portfolio, search, project-graph, and other context/query
  projections;
- the acquisition watcher, watch-folder configuration, retry queue, and local
  capture client;
- frontend, review, and other UI workflows.

Each needs its own contract and tests rather than implicitly changing existing
Dataset identity, export completeness, or context semantics here. Raw member
bytes remain in the acquisition system.
