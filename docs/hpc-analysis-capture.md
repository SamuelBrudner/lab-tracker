# HPC Analysis Capture

Lab Tracker can capture analysis provenance from Slurm/HPC workflows without
becoming a scheduler or copying large result directories. The HPC client writes
small JSON events to a local outbox first, then `lt hpc sync` turns those events
into staged evidence notes for the normal daily review and graph-draft flow.

The boundary is intentional: scheduler facts, git state, artifact pointers, log
excerpts, and metrics can be captured automatically. Claims, question links, and
analysis meaning still require human review.

`lt hpc` is the scheduler-aware adapter for the generic watch-folder capture
pattern described in [watch-folder-capture.md](watch-folder-capture.md). Use
`lt watch` for non-HPC evidence folders, acquisition-session outputs, and
generic manifests; use `lt hpc` when Slurm/HPC job lifecycle details matter.

## Setup

Run this once in the analysis repository or HPC checkout:

```powershell
lt hpc init --project <PROJECT_UUID> --cluster bouchet
```

This writes `.lab-tracker/hpc.json` and creates the default outbox at
`.lab-tracker/outbox/hpc/`.

Generic Slurm config:

```json
{
  "version": 1,
  "project_id": "PROJECT_UUID",
  "cluster": "my-cluster",
  "scheduler": "slurm",
  "outbox": ".lab-tracker/outbox/hpc"
}
```

Bouchet example:

```powershell
lt hpc init --project <PROJECT_UUID> --cluster bouchet --scheduler slurm
```

Use `LAB_TRACKER_HPC_CONFIG` when the config lives outside the current checkout,
and `LAB_TRACKER_HPC_OUTBOX` when the outbox should live on scratch.

## Capture Modes

### Submit Wrapper

Use the wrapper where you would normally run `sbatch`:

```bash
lt hpc submit -- sbatch analysis.sbatch
```

The wrapper records the command, run id, parsed Slurm job id, git commit, dirty
state, working directory, and stdout/stderr excerpt. It also exports
`LAB_TRACKER_HPC_RUN_ID`, `LAB_TRACKER_HPC_OUTBOX`, and `LAB_TRACKER_HPC_CONFIG`
to the submission command. Slurm's default `--export=ALL` behavior passes those
through to the job; if a site or script uses `--export=NONE`, pass the variables
explicitly.

### Script Hooks

Pipelines that can call a tiny hook may write lifecycle events directly:

```bash
lt hpc begin --run "$LAB_TRACKER_HPC_RUN_ID"
python run_analysis.py
status=$?
lt hpc finish --run "$LAB_TRACKER_HPC_RUN_ID" --exit-code "$status" \
  --artifact "file:///scratch/$USER/run-001/results.csv" \
  --metric "heldout_accuracy=0.91"
exit "$status"
```

`finish` can also include log excerpts:

```bash
lt hpc finish --run run-001 --exit-code 0 --log slurm-12345.out
```

### Watch Folders

For workflows that only drop outputs in a folder, write a manifest named
`lab-tracker-hpc-run.json` in each run directory:

```json
{
  "run_id": "run-001",
  "event_type": "finish",
  "summary": "Decoded stimulus identity from held-out trials.",
  "scheduler": {
    "job_id": "12345",
    "state": "completed",
    "exit_code": 0
  },
  "metrics": {
    "heldout_accuracy": 0.91
  },
  "artifacts": [
    {
      "uri": "file:///scratch/snb6/run-001/summary.png",
      "kind": "figure",
      "title": "Held-out decoding summary",
      "summary": "Accuracy by stimulus condition."
    }
  ]
}
```

Then scan the folder:

```bash
lt hpc watch --root /scratch/snb6/project-runs
```

The HPC watch command writes the same HPC outbox event format as the wrapper and
hook modes. Generic `lt watch --mode manifest` uses
`lab-tracker-evidence.json`; `lt hpc watch` keeps the HPC-specific
`lab-tracker-hpc-run.json` manifest for compatibility.

## Sync And Review

Sync outbox events when the login node or workstation can reach Lab Tracker:

```bash
lt hpc sync
```

Each event becomes an idempotent staged evidence note. The note contains:

- run id, event type, cluster, scheduler, job id, state, and exit code
- project id plus optional candidate question/dataset ids
- git commit and dirty state when available
- artifact pointers with titles and summaries
- compact log excerpts and metrics

Large outputs stay where they are. Lab Tracker stores paths, hashes, summaries,
and small text excerpts so the daily review writer has enough context without
turning Lab Tracker into object storage.

To ask Lab Tracker to propose graph changes for review:

```bash
lt hpc sync --request-draft
```

This calls the existing analysis graph draft endpoint for each synced note. It
does not commit analyses, claims, visualizations, or question links.

Check local state at any time:

```bash
lt hpc status
```

## Troubleshooting

- `HPC config not found`: run `lt hpc init` in the checkout or set
  `LAB_TRACKER_HPC_CONFIG`.
- Submitted jobs do not see `LAB_TRACKER_HPC_RUN_ID`: check whether the Slurm
  script or site config overrides environment export.
- Sync fails but events remain local: fix connectivity/authentication and rerun
  `lt hpc sync`; failed events are retryable.
- Draft creation fails: the evidence note may still be synced. Configure the
  graph draft provider, then rerun `lt hpc sync --request-draft`.
- Artifact paths are not readable from the Lab Tracker workstation: register
  the shared filesystem or remote as a data store and capture a canonical
  `store://<name>/<locator>` identity, or include a short artifact summary in
  the manifest. Bare `file://` and remote URIs remain provenance metadata and
  are never dereferenced by project-authored resolution.
