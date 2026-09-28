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
through to the job.

For a job the scheduler accepted, the wrapper also writes a **run manifest**
into the submit directory: `.lab-tracker/hpc-runs/job-<job id>.json` (with a
`.<cluster>` suffix when `sbatch --parsable -M` printed one). It records the
run id, job id, config path, resolved outbox, the explicit
`--project`/`--question`/`--dataset`/`--tag` values, and the `lt` executable
that submitted the job. Slurm sets `SLURM_SUBMIT_DIR` and `SLURM_JOB_ID` in
every job even under `--export=NONE`, so `lt hpc begin` and `lt hpc finish`
inside such a job find their run, config and outbox through the manifest when
`LAB_TRACKER_HPC_RUN_ID` is absent (array tasks use `SLURM_ARRAY_JOB_ID`). The
job still needs `lt` on its `PATH` or called by absolute path. The manifest is
written right after `sbatch` returns, so it is there long before any job can
start; a manifest that cannot be written (read-only submit directory) prints
one warning and never fails the submission. `.lab-tracker/` is host-local
scratch; keep it gitignored.

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

`finish` can also include log excerpts (the last 4,000 characters, with
tokens, passwords and `user:password@` URLs scrubbed):

```bash
lt hpc finish --run run-001 --exit-code 0 --log slurm-12345.out
```

### Slurm Epilog (No Job-Script Edits)

With the submit wrapper plus a cluster-wide `TaskEpilog`, every job submitted
through `lt hpc submit` records its finish without a single line in the job
script. Administrators install the template once;
users do nothing per job.

`lt hpc epilog` reads Slurm's environment (`SLURM_JOB_ID`,
`SLURM_ARRAY_JOB_ID`/`SLURM_ARRAY_TASK_ID`, `SLURM_SUBMIT_DIR` or
`SLURM_JOB_WORK_DIR`, `SLURM_CLUSTER_NAME`, and `SLURM_JOB_EXIT_CODE2` /
`SLURM_JOB_EXIT_CODE` where the epilog type provides them) and the run
manifest, then writes the same `finish` event `lt hpc finish` would, carrying
the submit's declared question, datasets and tags, the submit directory's git
state, and the tail of Slurm's default output file (`slurm-<job>.out` or
`slurm-<array job>_<task>.out` in the submit directory, or `--log`). It is:

- **idempotent** — a run (per array task) that already has a finish event,
  because the job called `lt hpc finish` itself or an earlier task epilog
  ran, is left alone (`"action": "already_finished"`), and the epilog's own
  event id is deterministic;
- **fail-silent** with `--fail-silent`, and a quiet no-op outside a job, for a
  job without a manifest (plain `sbatch`), or with
  `LAB_TRACKER_HPC_EPILOG_ENABLED=0`;
- **offline** — it only writes the user's outbox. The scheduled
  `lt watch run` / `lt hpc sync` on a login node drains it.

Install `scripts/slurm-task-epilog.sh` (all nodes):

```bash
install -m 0755 scripts/slurm-task-epilog.sh /etc/slurm/lab-tracker-task-epilog.sh
# slurm.conf
TaskEpilog=/etc/slurm/lab-tracker-task-epilog.sh
scontrol reconfigure
```

If the site already has a `TaskEpilog`, call the template from it. The
template acts only for the batch step (`SLURM_STEP_ID` unset or `batch`,
`SLURM_PROCID` 0) of jobs whose run manifest exists, runs the `lt` recorded in
the manifest (or `LAB_TRACKER_LT`, then `lt` on `PATH`) under a 60-second
`timeout`, discards its output, and always exits 0.

Which epilog, and why `TaskEpilog`:

| Slurm hook | Runs where, as whom | Sees | Fit |
| --- | --- | --- | --- |
| `Prolog` / `Epilog` | `slurmd` on each allocated node, as `SlurmdUser` (normally root) | job id, user, uid, partition; the exit code on recent releases; not the job's environment | Not recommended: it would read user-writable manifests and run a user-installed `lt` as root, a privilege-escalation path, and needs a `runuser` hop to write the user's outbox |
| `PrologSlurmctld` / `EpilogSlurmctld` | `slurmctld` on the controller, as `SlurmUser` | the job record, including `SLURM_JOB_EXIT_CODE(2)` | Not suitable: the controller usually cannot reach users' scratch or checkouts, must not run user code, and a slow script delays scheduling |
| `TaskProlog` / `TaskEpilog` | `slurmstepd` on the compute node, **as the job user**, once per task of each step, in the task's environment | `SLURM_JOB_ID`, `SLURM_SUBMIT_DIR`, array ids, cluster name — everything the job sees | **Recommended**: no privilege boundary is crossed, it reaches the user's own files, and it runs whether or not the job script calls `lt` |
| `SrunProlog` / `SrunEpilog`, `srun --task-epilog` | `srun`, as the user | the `srun` environment | Not suitable: `srun` steps only, and `--task-epilog` is per-job user effort |

Limitation: Slurm gives a `TaskEpilog` no exit status, so an epilog-written
finish records state `ended` with an unknown exit code (the summary says so).
Jobs that call `lt hpc finish --exit-code "$status"` themselves keep their exit
code; the epilog then does nothing. A site that prefers a privileged epilog
can pass the exit code with `--exit-code`, or rely on
`SLURM_JOB_EXIT_CODE2`/`SLURM_JOB_EXIT_CODE`, but must drop to the job user
first — check `man slurm.conf` for which variables your Slurm release sets in
which epilog.

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
- project id plus optional candidate question/dataset ids; the declared
  question and datasets are also attached as note targets, and the note's
  `declared_target_source` says whether the question came from a flag or
  manifest (`explicit`) or from the config's `default_question_id`
  (`config_default`) -- a stale id fails the sync loudly
- git commit and dirty state when available (a `git status` that times out after
  `LAB_TRACKER_GIT_TIMEOUT_SECONDS`, default 10 seconds, or fails is recorded as
  unknown, never as clean)
- on `begin` and `finish` events, the git tree id of the job's working copy
  (`hpc_git_worktree_tree`), the identity of the exact code the job ran even
  when it was never committed; a later `lt repo` commit with the same tree is
  proposed as the code the job derived from (see
  [run-capture.md](run-capture.md#code-identity-for-uncommitted-code))
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
- Submitted jobs do not see `LAB_TRACKER_HPC_RUN_ID`: expected under
  `--export=NONE`; `lt hpc begin`/`finish` fall back to the run manifest in
  the submit directory. If that fails, check that
  `.lab-tracker/hpc-runs/job-<job id>.json` exists there (`lt hpc submit`
  prints `run_manifest` or `run_manifest_error`).
- The epilog records nothing: only jobs submitted through `lt hpc submit`
  have a manifest; run `lt hpc epilog` by hand inside `salloc` with
  `SLURM_JOB_ID`/`SLURM_SUBMIT_DIR` set to see its JSON result.
- Pipelines on the cluster (Snakemake, Nextflow, Kedro, DVC) can record each
  run's declared inputs and outputs with `lt pipeline`; see
  [pipeline-capture.md](pipeline-capture.md).
- Sync fails but events remain local: fix connectivity/authentication and rerun
  `lt hpc sync`; failed events are retryable.
- Draft creation fails: the evidence note may still be synced. Configure the
  graph draft provider, then rerun `lt hpc sync --request-draft`.
- Artifact paths are not readable from the Lab Tracker workstation: register
  the shared filesystem or remote as a data store and capture a canonical
  `store://<name>/<locator>` identity, or include a short artifact summary in
  the manifest. Bare `file://` and remote URIs remain provenance metadata and
  are never dereferenced by project-authored resolution.
