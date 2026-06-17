# Evidence Source Metadata

Lab Tracker treats external evidence sources as inputs that create staged notes.
Adapters may harvest from synced folders, Google Drive desktop folders, Dropbox,
OneDrive, CI outputs, git checkouts, or other tools, but they should only create
raw evidence notes. They must not create canonical graph records or commit graph
draft operations.

Use these note metadata keys for imported evidence:

| Key | Meaning |
| --- | --- |
| `evidence_source_provider` | External system name, such as `local-folder`, `google-drive`, or `ci`. |
| `evidence_source_uri` | Stable source URI or absolute file URI for the item. |
| `evidence_source_external_id` | Provider-specific item ID, or root-relative path for local folders. |
| `evidence_source_observed_at` | ISO-8601 timestamp when the adapter observed the item. |
| `evidence_capture_kind` | Evidence kind, such as `file`, `text`, or `analysis_evidence`. |
| `evidence_content_hash` | SHA-256 hash of the imported evidence bytes or text. |
| `evidence_adapter` | Adapter or script name/version that created the note. |
| `evidence_title` | Human-readable title for reports and review screens. |

The local-folder adapter imports files as staged note assets:

```bash
lt import-folder \
  --project "$LAB_TRACKER_PROJECT_ID" \
  --root "$HOME/Library/CloudStorage/GoogleDrive/My Drive/lab-inbox" \
  --include "*.pdf" \
  --include "*.md"
```

The adapter uses the root-relative POSIX path as `evidence_source_external_id`
and the absolute `file://` URI as `evidence_source_uri`. It skips an item when a
note already exists with the same project, source provider, external ID, and
content hash. Changed file contents therefore create a new staged evidence note.

`lt import-folder` imports files as staged evidence notes that record where each
file came from. Imported notes never become canonical graph records on import,
and graph changes are never committed automatically — humans review the staged
notes, and human review remains the commit boundary.

> Deferred / future, not part of v1: automated batch drafting that proposes
> reviewable graph changes from staged notes could be layered on later, but it is
> not shipping today.
