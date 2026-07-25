# Acquisition Collection Contract

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
snapshot. Dataset commits may reference only complete snapshots.

Capture retries are idempotent by client capture ID and content hash. An older
offline observation may remain in history but cannot replace a newer current
snapshot. Two different manifests observed at the same timestamp are a
conflict rather than an arbitrary ordering choice.

Content identity and capture observations are stored separately. Every capture
receipt retains its client capture ID, observation time, reported completeness,
source URI/provider, and acting-principal provenance. An unchanged rescan
therefore reuses the immutable manifest snapshot while advancing the
collection's current observation pointer. Compact collection and Dataset reads
use that selected observation's source, time, capture ID, and principal facts;
the snapshot's completeness remains a monotonic seal and cannot be reversed by
a later incomplete report.

Lab Tracker keeps a durable database copy of canonical manifest JSON, loaded
only by manifest/member endpoints. Summary, Dataset, project graph, and default
PROV responses contain only IDs, hashes, counts, bytes, source facts, and
observation time.

## Experiment and Dataset identity

`Experiment` is a lightweight semantic grouping with one required primary
Question and independent many-to-many Session and Dataset memberships. It does
not model trials, protocols, conditions, samples, wells, instruments, or
metrics. Membership links remain outside Dataset content identity.

Committed Dataset manifests reference collection snapshots compactly. New
Dataset hashes include sorted `(collection_key, manifest_hash)` pairs only when
collections are present, so every legacy manifest retains its existing hash.
Raw member bytes remain in the acquisition system.

For deployment order, client compatibility, device scope, and recovery, see
[Acquisition Collections: Use and Rollout](acquisition-collections.md).
