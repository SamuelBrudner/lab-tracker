# Pipeline, CI, and Cluster Capture

Pipeline frameworks already know what a run *declares*: Snakemake's jobs name
their inputs and outputs, Kedro's pipelines have free inputs and outputs in a
data catalog, DVC's `dvc.lock` hashes every stage's dependencies and outputs,
and Nextflow's trace lists every task. `lt pipeline` turns that declaration
into **one staged Lab Tracker note per pipeline run**, so a lab that runs a
framework gets the epistemic layer (question → run → evidence) with no
double-entry.

This is the framework-hook integration shape recorded in
[build-vs-buy-boundaries.md](build-vs-buy-boundaries.md#pipeline-and-lineage-boundary):

- Only **declared** inputs and outputs are recorded, as pointers (URI, sha256
  when cheap, size). Lab Tracker builds no catalog, runs nothing, copies no
  bytes, and never intercepts reads.
- The mechanical file-to-file DAG (intermediates) stays with the framework.
  A captured output's sha256 is the cross-tool join key: when the same bytes
  later appear as another run's input, review can propose the link.
- The note is **staged**. Nothing is committed, and the only targets are the
  question, session and datasets a person declared.

The same page covers the reusable GitHub Action for commit capture in CI
([CI capture](#ci-capture-github-action)) and points to the Slurm epilog
([HPC analysis capture](hpc-analysis-capture.md#slurm-epilog-no-job-script-edits)).

## Setup

1. Install the `lab-tracker` client (`lt`) in the environment the pipeline
   runs in (Snakemake/Kedro load it in-process; Nextflow and DVC call `lt`).
2. Bind the project once per checkout: `lt project bind` (writes
   `lt_ids.json`), or export `LAB_TRACKER_PROJECT_ID`, or pass `--project`.
   A run in a checkout with no bound project is **skipped** with one stderr
   notice — it never lands in a default project. The checkout's
   `.lab-tracker/watch.json`, `repo.json` or `hpc.json` project also counts
   as a binding.
3. Add the one-time hook for your engine (below).
4. Optional: `lt setup schedule` so `lt watch run` drains queued runs even
   when the pipeline host was offline.

After that the per-run effort is **zero**: every run records itself.

## What One Run Records

Each run writes a single event to the checkout's watch outbox
(`<checkout>/.lab-tracker/outbox/watch`, `capture_kind` `pipeline_run`,
adapter `lt-pipeline`) and then drains that outbox best-effort. It syncs as a
staged markdown note containing:

- engine (`snakemake`, `nextflow`, `kedro`, `dvc`, `generic`), status
  (`success`, `error`, `unknown`), run id, label, start and end time when
  known;
- **declared inputs and outputs** as pointers: `file://` URI, title relative to
  the checkout, `sha256:` content hash for files up to the hashing cap
  (default 64 MiB, `--hash-max-bytes`) within a per-run budget of 512 MiB
  (outputs are hashed first), size only above it, directories summarized
  (file count and total size, walk capped at 10,000 entries, never hashed),
  remote URIs (`s3://`, `gs://`, `https://`) as credential-free pointers
  that are never dereferenced, and missing paths marked as such. At most 25
  inputs and 25 outputs are listed (`--max-artifacts`); the rest are counted
  ("… and N more outputs");
- git HEAD, dirty flag (unknown, never "clean", when git cannot answer),
  branch, and the credential-free `origin` remote;
- a log excerpt: the last 4,000 characters of `--log` files plus up to 1,000
  characters of an error message, with tokens, passwords, bearer headers and
  `user:password@` URLs scrubbed;
- engine details (jobs per rule, failed tasks, DVC stage commands and
  params), bounded.

The note body is capped at 60,000 characters. Note metadata carries
`pipeline_engine`, `pipeline_status`, `pipeline_run_id`, `pipeline_label`,
`pipeline_started_at`, `pipeline_ended_at`, `pipeline_input_count`,
`pipeline_output_count`, `pipeline_inputs_omitted`, `pipeline_outputs_omitted`,
`pipeline_git_remote`, `pipeline_project_source`, engine-specific
`pipeline_<engine>_*` keys, and the usual `git_commit` / `git_dirty` /
`git_branch` (so a run links to an analysis at that commit through the
existing exact-id proposals) and `watch_*` capture keys.

One run is one event: reporting the same engine and run id again in the same
checkout is a no-op (`"action": "already_captured"`).

## Generic: `lt pipeline report`

Any pipeline, or a wrapper script, can call the generic verb at the end of a
run:

```bash
lt pipeline report --engine generic --status success \
  --input data/raw.csv --output results/ --output @outputs.txt \
  --run-id "$RUN_ID" --log pipeline.log --label "nightly QC" --fail-silent
```

| Option | Meaning |
| --- | --- |
| `--engine` | `snakemake`, `nextflow`, `kedro`, `dvc` or `generic` (default) |
| `--status` | `success`, `error` or `unknown` (default) |
| `--input` / `--output` | declared path, directory or URI; repeatable; `@FILE` reads one path per line (`#` comments allowed) |
| `--run-id` | engine run id (default: generated) |
| `--started-at` / `--ended-at` | ISO-8601 times (stored as UTC) |
| `--log` | log file whose tail is excerpted; repeatable |
| `--project` / `--session` / `--question` | declared project, session (UUID or `LT-` link code; default: the checkout's active session) and question target |
| `--label`, `--summary`, `--tag` | human context |
| `--cwd` | pipeline working directory; relative paths resolve here |
| `--hash-max-bytes`, `--max-artifacts` | the caps described above |
| `--no-drain` | queue only; skip the best-effort sync |
| `--request-draft` | ask for a graph draft for this run's note when it syncs |
| `--fail-silent` | exit 0 with no output on any error — use it in hooks |

Without a configured server (`LAB_TRACKER_BASE_URL` or a saved connection
profile) the event is only queued: no network call is made. When the drain
fails the event stays queued, one stderr line says so, and `lt outbox sync`
(or the scheduled `lt watch run`) retries it.

## Snakemake

Add to the Snakefile once:

```python
onsuccess:
    try:
        from lab_tracker_client.integrations.snakemake import report
        report(log, status="success")
    except Exception:
        pass

onerror:
    try:
        from lab_tracker_client.integrations.snakemake import report
        report(log, status="error")
    except Exception:
        pass
```

Snakemake hands these handlers `log`, the run's log file
(`.snakemake/log/<start>.snakemake.log`). `report` derives the declared
inputs and outputs from it: outputs are the outputs of jobs that finished;
inputs are the executed jobs' inputs that no executed job produced
(aggregate target rules without outputs, like `rule all`, are ignored). When
the log names no jobs (`--quiet`), it falls back to the `.snakemake/metadata`
records whose end time is after the run started. Explicit `inputs=` and
`outputs=` arguments override both. The run id and start time come from the
log's file name; `workflow=workflow` adds the Snakefile path. `report` accepts
the same `project`, `question`, `session`, `label`, `drain` and
`request_draft` keywords as the CLI options and never raises. Paths containing
`", "` cannot be split from the log; pass them explicitly.

Shell alternative: `shell("lt pipeline report --engine snakemake --status
success --log {log} --output results --fail-silent || true")`.

## Nextflow

Record each run from the workflow itself (`main.nf` or `nextflow.config`):

```groovy
workflow.onComplete {
    def cmd = [
        'lt', 'pipeline', 'report', '--engine', 'nextflow',
        '--status', workflow.success ? 'success' : 'error',
        '--run-id', "${workflow.sessionId}-${workflow.runName}".toString(),
        '--started-at', workflow.start.toString(),
        '--ended-at', workflow.complete.toString(),
        '--output', params.outdir.toString(),
        '--log', '.nextflow.log',
        '--label', workflow.runName.toString(),
        '--fail-silent',
    ]
    try {
        def proc = cmd.execute(null, new File(workflow.launchDir.toString()))
        proc.consumeProcessOutput()
        proc.waitForOrKill(120000)
    } catch (Exception e) {
        log.debug "Lab Tracker capture skipped: ${e.message}"
    }
}
```

The run id combines `sessionId` (kept across `-resume`) with `runName` (new
for every launch), so a resumed run is a new record. Adjust `--output` to the
pipeline's `publishDir`; it is summarized as a directory pointer.

For task-level status, record the run from its trace file instead, after
`nextflow run` returns (or from a CI step):

```bash
nextflow run main.nf -with-trace results/pipeline_info/trace.txt
status=$?
lt pipeline nextflow --trace results/pipeline_info/trace.txt \
  --status "$([ "$status" -eq 0 ] && echo success || echo error)" \
  --output results --log .nextflow.log --fail-silent
exit "$status"
```

`lt pipeline nextflow` reads the tab-separated trace (any column order; it
needs `name` and `status`), uses each task's **last** attempt (a retried
failure counts as its retry's outcome), counts completed, cached, failed and
aborted tasks per process, names the failed tasks, and takes the first
`submit` and last `complete` times when those columns exist. Without
`--status` the status is `error` if any task's last attempt failed; a
workflow using `errorStrategy 'ignore'` can succeed anyway, so pass the real
status when you have it. The trace does not list published files — pass them
(or the `publishDir`) with `--output`.

## Kedro

Register the hooks in the Kedro project's `src/<package>/settings.py`:

```python
from lab_tracker_client.integrations.kedro import LabTrackerHooks

HOOKS = (LabTrackerHooks(),)
```

`LabTrackerHooks` implements Kedro's `before_pipeline_run`,
`after_pipeline_run` and `on_pipeline_error` hook specs without importing
Kedro (its methods carry the `kedro_impl` marker Kedro's `hook_impl` sets).
After a run, or when it fails, it records the pipeline's **free** inputs
(`pipeline.inputs()`) and free outputs (`pipeline.outputs()`), resolved to the
catalog datasets' file paths: versioned datasets resolve to the concrete
version path, remote datasets (`s3://…`) are pointers, and in-memory datasets
and parameters are counted but not recorded. Intermediate datasets are
Kedro's mechanical lineage and stay with Kedro. The Kedro session id is the
run id; the pipeline name, environment and Kedro version go into metadata; a
failure's exception message goes into the excerpt. Keyword arguments
(`project=`, `question=`, `session=`, `label=`, `drain=`, `request_draft=`)
mirror the CLI. The hooks never raise into the Kedro run.

## DVC

Record the pipeline state `dvc.lock` describes, typically right after
`dvc repro`:

```bash
dvc repro && lt pipeline dvc --status success --fail-silent
```

`lt pipeline dvc [--lock dvc.lock]` records each stage's command (bounded and
credential-scrubbed), params, dependencies and outputs. Pointers carry DVC's
own hash — `md5:<hex>` (a directory's hash ends in `.dir`), or `etag:` /
`checksum:` for cloud entries — and size; nothing is re-hashed. Outputs are
every stage's `outs`; declared inputs are the `deps` no stage produces. The
default run id is derived from the lock file's content, so reporting an
unchanged lock twice records one run. Paths resolve relative to the lock
file's directory (a stage `wdir` from `dvc.yaml` is not in the lock).

`dvc.lock` is YAML and PyYAML is not a Lab Tracker dependency: the client
uses PyYAML when it is installed and otherwise a built-in reader for the
block-style YAML subset DVC writes (`schema: '2.0'` and the older
top-level-stages layout). Anchors, aliases and tags are rejected rather than
guessed.

## CI Capture (GitHub Action)

`.github/actions/lab-tracker-repo-report` is a composite action that installs
the client from this repository at a pinned ref (it is not on PyPI) and runs
the existing `lt repo report --fail-silent` for the pushed commit. It never
fails the workflow: a missing ref, server, token or project, an install
failure, or an unreachable server each become one annotation.

Example workflow for an analysis repository (not part of this repository's
own workflows):

```yaml
name: lab-tracker capture
on:
  push:
    branches: [main]
  pull_request:

jobs:
  capture:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          # The pushed commit, not GitHub's pull_request merge commit.
          ref: ${{ github.event.pull_request.head.sha || github.sha }}
          # The parent, so the commit's bounded diff renders.
          fetch-depth: 2
      - uses: SamuelBrudner/lab-tracker/.github/actions/lab-tracker-repo-report@<pinned-sha>
        with:
          lab-tracker-ref: <pinned-sha>        # or a release tag
          base-url: ${{ vars.LAB_TRACKER_BASE_URL }}
          access-token: ${{ secrets.LAB_TRACKER_ACCESS_TOKEN }}
          project-id: ${{ vars.LAB_TRACKER_PROJECT_ID }}
          include-pr-text: "true"              # PR title/body as the summary
```

Inputs: `lab-tracker-ref` (required; pin a tag or full SHA),
`lab-tracker-repository`, `base-url`, `access-token`, `project-id` (each
falls back to `LAB_TRACKER_BASE_URL`, `LAB_TRACKER_ACCESS_TOKEN`,
`LAB_TRACKER_PROJECT_ID` in the job environment; the project also to the
repository's `lt_ids.json`), `question-id`, `commit` (default: the PR head or
pushed SHA; the checked-out HEAD must match), `remote-url`,
`include-pr-text` and `pr-text-max-chars` (default 2,000 characters,
credential-scrubbed), `python`, `working-directory`. Every input reaches the
scripts through step environment variables, never interpolated into shell
code; the token is only exported for `lt` and never printed. Pull requests
from forks get no secrets, so their runs skip with a warning.

### One commit, one note: how CI and the hook dedupe

Both paths call `lt repo report`, so both derive the commit's evidence
identity with the same code (`repo.event_source_external_id`):

```
<normalize_remote(sanitize_remote_url(remote.origin.url))>@<full sha>
```

`normalize_remote` drops the scheme, credentials and a `.git` suffix, turns
scp-style `host:path` into `host/path` and lowercases, so a laptop clone of
`git@github.com:Lab/Analysis.git` and the runner's
`https://github.com/Lab/Analysis` both yield
`github.com/lab/analysis@<sha>` (the same identity
`scripts/create-analysis-graph-draft.py` emits). That identity is both the
note's `evidence_source_external_id` and its `client_capture_id`, and the
server keeps one note per `(project, client_capture_id)`: whichever capture
arrives first creates the note, and the other — whose content differs (host,
branch, annotations) — is refused with HTTP 409 ("already used with different
field(s)") instead of creating a parallel note. The action reports that
refusal as a notice, not a failure.

Limits:

- An SSH host alias (`git@gh-work:lab/analysis`), a mirror, or an
  `ssh://host:port/...` origin normalizes differently. Set `remote-url` to the
  committing machines' origin; the action applies it to `lt`'s own git calls
  through `GIT_CONFIG_*` environment variables and leaves the checkout alone.
- If CI captures a commit before a laptop's queued hook event syncs, that
  local event is refused the same way and stays `failed` in
  `lt outbox status`. It is a duplicate of the CI note and safe to delete.
- The runner's outbox is discarded with the job: a capture that cannot reach
  the server is lost (the hook remains the durable path).
- Only the pushed head commit is captured; merge commits are skipped by the
  same default commit filter the hook uses.
- The action's install step expects a Linux or macOS runner.

## Kill Switches

- `LAB_TRACKER_PIPELINE_CAPTURE=0` (or `false`/`no`/`off`) turns off
  `lt pipeline` and the Snakemake and Kedro adapters everywhere, without
  editing the pipeline.
- `--no-drain` / `drain=False` keeps capture local.
- Remove the Snakefile handlers, the `HOOKS` entry, the `onComplete` block,
  or the workflow step to uninstall.

## Troubleshooting

- `pipeline run not recorded: no project is bound`: run `lt project bind` in
  the checkout, export `LAB_TRACKER_PROJECT_ID`, or pass `--project`.
- `pipeline run … is queued`: the server was unreachable; `lt outbox status`
  shows it and `lt outbox sync` retries.
- Many outputs listed as "Pointer only": raise `--hash-max-bytes`, or accept
  pointers for large files — the hash is what joins runs across machines.
- Snakemake records no outputs: the log had no job blocks and no
  `.snakemake/metadata` records ended after the run started; pass
  `outputs=` explicitly.
