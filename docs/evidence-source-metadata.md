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

`--include` and `--exclude` are both repeatable relative-path globs (omit
`--include` to match all files). Other options:

- `--dry-run`: report what would be imported without creating notes.
- `--limit <n>`: cap the number of matched files processed.
- `--provider <name>`: evidence source provider name (default: `local-folder`).
- `--adapter-name <name>`: adapter identifier recorded in metadata (default:
  `lt-import-folder`).
- `--status <staged|committed|archived>`: note status for imported files
  (default: `staged`).

The adapter uses a root-qualified POSIX path as `evidence_source_external_id`
and the absolute `file://` URI as `evidence_source_uri`. The external ID is
formatted as `<root-uri>::<relative-path>` so two imported folders with the same
file names do not share one dedupe namespace. It skips an item when a note
already exists with the same project, source provider, external ID, and content
hash. Changed file contents therefore create a new staged evidence note.
Symlinked files are skipped during discovery and are not followed outside the
configured import root.

`lt import-folder` imports files as staged evidence notes that record where each
file came from. Imported notes never become canonical graph records on import,
and graph changes are never committed automatically — humans review the staged
notes, and human review remains the commit boundary.

Shipped retained-v1 batch graph drafting can propose reviewable graph changes
from staged notes, either on the configured cadence or from a user-triggered run.
Those drafts still require human edit, accept, or reject review before any graph
commit.
