# Evidence Source Metadata

Lab Tracker treats external inboxes as evidence sources that create notes. Adapters
may harvest from Google Drive, synced folders, git, CI, or other tools, but they
should only create raw evidence notes. They must not commit canonical graph changes.

Use these note metadata keys for imported evidence:

| Key | Meaning |
| --- | --- |
| `evidence_source_provider` | External system name, such as `generic`, `git`, or `github-actions`. |
| `evidence_source_uri` | Stable source URI or path for the item. |
| `evidence_source_external_id` | Provider-specific ID, commit SHA, run ID, or source URI fallback. |
| `evidence_source_observed_at` | ISO-8601 timestamp when the adapter observed the item. |
| `evidence_capture_kind` | Evidence kind, such as `text`, `file`, `git_commit`, or `analysis_evidence`. |
| `evidence_content_hash` | SHA-256 hash of the imported evidence bytes or text. |
| `evidence_adapter` | Adapter or script name/version that created the note. |
| `evidence_title` | Human-readable title for reports and review screens. |

The generic importer stores a text note or raw file note without requesting graph
drafts:

```bash
python scripts/import-evidence-note.py \
  --project-id "$PROJECT_ID" \
  --file /path/to/evidence.md \
  --source-provider google-drive \
  --source-uri "gdrive://folder/item-id"
```

Daily graph reviews can then create or reuse note-scoped graph drafts and generate
the scientist-facing review brief from the accumulated evidence.
