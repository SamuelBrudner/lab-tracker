# Artifact capture from Python

Lab Tracker can stage a regular-file output at the moment your code writes it.
The write still belongs to your analysis code and the full file stays where you
put it. Lab Tracker receives a bounded review payload or an external pointer,
the output URI and content hash, and a pointer back to the code that produced
it.

Use `save_artifact` when you control the write call:

```python
from lab_tracker_client import save_artifact

save_artifact(
    "results/summary.parquet",
    kind="analysis-table",
    writer=lambda path: dataframe.to_parquet(path, index=False),
)
```

The same wrapper works for binary outputs:

```python
import joblib

save_artifact(
    "models/classifier.joblib",
    kind="model",
    writer=lambda path: joblib.dump(model, path),
)
```

For figures, `savefig` is the convenient equivalent and keeps the bounded
review-image behavior:

```python
from lab_tracker_client import savefig

savefig(fig, "figures/tuning-summary.png")
```

The writer receives an absolute `path`. If it fails, its exception propagates
and Lab Tracker stages nothing. After a successful write, capture is fail-soft:
an unavailable Lab Tracker does not delete the output or turn a successful
analysis write into a failure.

## Exact producer provenance

Each eager `save_artifact` or `savefig` call records `producer_*` metadata for
that individual write:

- repo-relative source file, exact line, and enclosing symbol;
- a hash of the source region around that line;
- the source repository's HEAD commit, dirty state, and credential-stripped
  origin URL when available; and
- the observation time.

The commit is the checked-out base revision. When the tree is dirty, the source
region hash distinguishes the code actually observed near the save line from
the committed base. Output identity remains separate in
`evidence_source_uri` and `evidence_content_hash`.

## Capturing writes you cannot wrap

Use `capture` for a bounded directory and set of file patterns:

```python
from lab_tracker_client import capture

with capture(
    "results",
    patterns=["*.csv", "*.parquet"],
    kind="analysis-output",
):
    run_pipeline()
```

This scan can identify the enclosing `with` statement but cannot know which
line inside `run_pipeline` wrote each file. Its records therefore use honest
`capture_scope_*` metadata instead of fabricated `producer_*` metadata.

When some writes inside a capture block are under your control, use the context
method for exact attribution and let the exit scan find the rest:

```python
with capture("results", patterns=["*.csv"], kind="analysis-output") as staged:
    staged.save_artifact(
        "results/summary.csv",
        writer=lambda path: dataframe.to_csv(path, index=False),
    )
    legacy_pipeline()
```

The eager output is not captured a second time when the context exits.
`capture_figures` is the image-pattern convenience form. For directory stores,
multi-file output trees, or workflow-scale discovery, use `lt watch` rather
than treating a directory as one artifact.

## Staging boundary

Artifact helpers create staged evidence notes only. They do not decide that a
file is a canonical Dataset, Analysis, Claim, or Visualization. A person adds
that graph meaning during review. Large or unsupported outputs become
pointer-only notes; their full bytes remain in the consumer repository or
artifact store.
