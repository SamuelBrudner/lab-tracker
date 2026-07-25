# Acquisition Collections: Use and Rollout

Use an acquisition collection when one experimental run produces hundreds or
thousands of files that are scientifically one body of data. Lab Tracker keeps
one logical collection attached to a Session, an immutable content-addressed
manifest snapshot, and a compact reference from a committed Dataset. It does
not turn every trial file into a database row or graph node, and it does not
copy the raw bytes out of the acquisition system.

This feature is opt-in. Existing `staged-note` and `acquisition-output` watches,
queued outbox events, Sessions, Datasets, and Dataset hashes continue to work
without conversion.

For the normative manifest and identity rules, see the
[acquisition collection contract](acquisition-collection-contract.md). For the
commands and watch configuration, see
[watch folder capture](watch-folder-capture.md).

## Model at a glance

- An **Experiment** groups the scientific work around one immutable primary
  Question. Sessions and Datasets can belong to more than one Experiment.
- A **Session** records the acquisition activity.
- An **acquisition collection** is unique by Session and stable
  `collection_key`.
- A **snapshot** records one immutable manifest-content version; an incomplete
  snapshot can only be sealed as complete. Separate capture receipts retain
  each observation’s time, source, completeness report, and principal.
- A **Dataset** stores a compact snapshot reference. Member files stay inside
  the managed manifest and raw file bytes stay at the source.

Collection mode is deliberately not a protocol, trial, condition, sample,
well, instrument, inventory, metrics, or object-storage model.
Execution metrics, parameters, artifact browsing, and run UI remain on the
external side of the
[build-vs-buy boundary](build-vs-buy-boundaries.md).

## Limits and storage behavior

Manifest v1 accepts at most **10,000 members** and a **16 MiB request**. The
exact collection-capture POST path rejects an oversized `Content-Length` early
and otherwise counts streamed chunks up to that bound before Pydantic body
validation, including when the request is chunked or has no length header. The
server also checks the canonical manifest against the 16 MiB bound. Each member
contains a normalized relative POSIX path, a lowercase SHA-256 checksum, and a
nonnegative 64-bit size. The collection total uses a database `BigInteger`, so
individual files and totals above 2 GiB round-trip without narrowing.

Lab Tracker stores canonical manifest JSON in a separate managed database value.
Ordinary collection, Dataset, graph, and provenance responses contain compact
metadata only. Member browsing parses the manifest on demand and returns the
requested page; the UI requests 100 members only after a person expands a
collection. The full canonical manifest is available as an explicit download.

This design keeps database and graph record counts constant with member count:
a new one-snapshot capture creates one logical collection when needed, one
snapshot, one manifest value, and one idempotency receipt—not 10,000 member
records.

## Server-first rollout

Roll out additively in this order:

1. **Back up the database and managed app data.** Follow
   [self-hosted operations](self-hosted-operations.md#backup) for Postgres or
   run `lab-tracker backup` for SQLite.
2. **Upgrade the server first.** Apply the current package/image and run:

   ```bash
   uv run alembic upgrade head
   ```

   The additive migrations create Experiments and their membership tables,
   acquisition collection/snapshot/manifest/capture tables, and the compact
   Dataset manifest column. They do not rewrite or backfill existing records.
3. **Verify the capability before enabling collection watches.**

   ```bash
   curl -fsS "${LAB_TRACKER_BASE_URL%/}/schema/describe"
   ```

   The response's `data.capabilities` list must contain
   `acquisition_collections_v1`.
4. **Upgrade clients and the UI.** Install the matching Lab Tracker package on
   acquisition machines. In enrolled consumer repositories, run `lt update`
   after upgrading the package, then `lt doctor`.
5. **Opt in one acquisition folder at a time.** Start with an incomplete
   snapshot, inspect the collection in the Session UI, then explicitly seal a
   stable scan with `--complete`.

The server-first order matters because a new collection-aware client never
degrades a collection into thousands of per-file requests.

## Opt-in watch configuration

For a one-off scan:

```bash
lt watch scan \
  --mode files \
  --sink acquisition-collection \
  --root /data/rig2/session-001 \
  --session <SESSION_UUID> \
  --collection rig2-session-001

lt watch sync
```

For a repeatable watch:

```bash
lt watch add /data/rig2/session-001 \
  --sink acquisition-collection \
  --session <SESSION_UUID> \
  --name rig2-session-001
```

The configured `name` becomes the immutable collection key. Collection watches
require `mode=files`, a Session ID, and a name. Keys are 1–120 characters, start
with a letter or digit, and contain only letters, digits, periods, underscores,
or hyphens. Add `--complete` only after the folder represents a complete
acquisition. Completeness is explicit; Lab Tracker does not infer it from
folder quiet time.

A scan fingerprints every member once and writes one durable local outbox
event. `lt watch run` can reuse that stable pass for its immediate sync. A later
command or retry rehashes every member before upload; a changed, missing, or new
file makes the event stale so it cannot silently register different content.

Committed legacy file references are available lazily through
`GET /datasets/{dataset_id}/manifest-files`; collection members remain on the
separate bounded snapshot-members endpoint.

## Promotion and reads

Automatic Session promotion includes every current collection snapshot.
Promotion stops and names current collections that are still incomplete. Seal
those collections or deliberately capture a different complete snapshot before
retrying.

When legacy acquisition outputs overlap collection members:

- the same path and checksum is represented only by the collection;
- the same path with a different checksum blocks promotion;
- unrelated legacy outputs remain ordinary Dataset files.

Experiment membership does not change Dataset content identity. Promotion keeps
the requested primary Question, adds parent Experiment Questions as secondary
links, and inherits every Session–Experiment membership onto the Dataset.

Use the Session and Dataset UI for compact collection summaries. Member pages
and path search are lazy. For API clients:

- `GET /sessions/{session_id}/collections` lists compact current summaries;
- `GET /collections/{collection_id}/snapshots` lists snapshot history;
- `GET /collection-snapshots/{snapshot_id}/members?limit=100&offset=0&q=...`
  reads a bounded page;
- `GET /collection-snapshots/{snapshot_id}/manifest` downloads the full
  canonical manifest;
- `GET /datasets/summaries` returns Dataset counts and identifiers without the
  full commit manifest.

## Capability and version compatibility

| Client | Server | Behavior |
| --- | --- | --- |
| Older client | New server | Existing per-file, note, and manifest events keep working. Collection mode is not selected. |
| New client | New server | The client sees `acquisition_collections_v1` and sends one snapshot request. |
| New client | Older server | The collection event remains queued with upgrade guidance. No per-file fallback is attempted. |
| Existing data | New server | Records remain unchanged. No acquisition outputs are backfilled into collections automatically. |

Dataset hashing is backward compatible. A manifest with no collection
references is hashed byte-for-byte as before. For a Dataset with collections,
identity adds only sorted `collection_key` and `manifest_hash` pairs; database
IDs, Experiment links, source URIs, timestamps, counts, and totals do not affect
the hash.

## Paired-device scope

Paired-device tokens retain read access and can `POST` only to the established
capture endpoints plus the new collection snapshot endpoint:

```text
/sessions/{session_id}/collections/{collection_key}/snapshots
```

They cannot create or mutate Projects, Questions, Experiments, Sessions,
Datasets, analyses, or other unrelated records. A snapshot records the acting
user when available and the actual principal type, instance ID, and label, so a
device upload remains attributable without widening device authority.

## Rollback and recovery

Disabling collection watches is safe: leave their unsynced event files in the
local outbox and re-enable them after the server is healthy. Do not convert
those events to per-file events.

Do not use an in-place Alembic downgrade as an operational rollback after
collection or Experiment data has been written. The downgrade drops the new
tables and Dataset collection-reference column. Instead:

1. stop capture and the application;
2. preserve every client outbox;
3. restore the pre-upgrade database and app-data backup together;
4. restore the prior application version;
5. restart and verify existing legacy capture;
6. upgrade server-first again before retrying queued collection events.

If only a client rollout is faulty, roll back or disable the client-side watch
configuration while leaving the additive server schema in place. Existing
collections remain readable, and legacy capture continues independently.

## Operating checks

After deployment, verify:

- `/schema/describe` advertises `acquisition_collections_v1`;
- one small incomplete collection appears under its Session;
- sealing an unchanged rescan reuses the content snapshot;
- an unsupported server leaves the outbox event queued;
- member paging returns only the requested page;
- automatic promotion names incomplete collections;
- a paired-device token can capture a snapshot but cannot mutate a Session;
- backups include the primary database, because the durable manifests live
  there even though raw member bytes do not.
