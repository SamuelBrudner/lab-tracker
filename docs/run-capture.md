# Run Capture (`lt run`)

`lt run` is the local analogue of `lt hpc submit`: put it in front of any
command and the run is recorded as one staged evidence note, without changing
how the command behaves.

```bash
lt run -- python analysis.py --config configs/decoder.yaml
```

The command runs exactly as it would without the prefix: same stdin, stdout,
and stderr, same working directory and environment, and `lt run` exits with
the command's own exit code. What ran -- the command line (secrets redacted),
the git state including the identity of *uncommitted* code, the environment
fingerprint, and pointers to the files the run wrote -- lands in the normal
daily review as a staged note. Nothing is committed: a person still decides
what the run means.

## Setup

Once per checkout, bind it to a project so captures know where they belong:

```bash
lt project bind --project-id <PROJECT_UUID> --yes   # writes lt_ids.json at the checkout root
```

`LAB_TRACKER_PROJECT_ID` in the environment or `--project <uuid>` on the
command work too. Only a *bound* project is captured; a project named only by
the checkout's watch config does not count (see
[watch-folder-capture.md](watch-folder-capture.md)), so a run never lands in a
default project it was not meant for. Gitignore `.lab-tracker/` (host-local
scratch; the outbox lives there). To sync runs immediately instead of on the
next scheduled drain, connect the client once (`lt setup connect`).

After that, the per-run effort is the `lt run --` prefix.

## Usage

```bash
lt run [--project UUID] [--session UUID|LT-CODE] [--question UUID] \
       [--label TEXT] [--output DIR]... [--no-drain] [--request-draft] \
       -- <command> [args...]
```

- `--label` names the run; it becomes the note title (`lt run: <label>`).
- `--output DIR` (repeatable) declares a folder, or a single file, whose
  created or modified files are recorded as artifact pointers. Relative paths
  are resolved against the working directory.
- `--session` attaches the note to an acquisition session (UUID or the link
  code a bench phone scans); `--question` attaches it to a candidate question.
  Both are declared note targets (`declared_target_source: explicit`). Without
  `--session`, the checkout's active session from `lt session use` rides along
  and the sync attaches it only when it belongs to the same project, exactly as
  for watched files. A `--session` value that is not a UUID or a valid link
  code is dropped with one notice; the run is still captured.
- `--no-drain` only queues the run; `--request-draft` asks for a graph draft of
  this run's note (and only this run's) when it syncs.

Examples:

```bash
lt run --label "decoder sweep" --output results --output figures -- python sweep.py
lt run --session LT-<code> --output /data/rig2/session014 -- matlab -batch run_pipeline
lt run -- make figures 2>&1 | tee build.log   # pipes and redirects behave as without it
```

## What Is Recorded

One watch-outbox event per run (`capture_kind: command_run`, adapter `lt-run`,
`sink: staged-note`), synced into one staged note whose markdown body carries
the details and whose metadata carries scalar `run_*` keys:

| Recorded | Note metadata |
| --- | --- |
| Command line, secrets redacted (at most 500 characters in metadata; the body keeps the full redacted line) | `run_command` |
| Working directory | `run_cwd` |
| Start and end (UTC ISO-8601), duration in seconds | `run_started_at`, `run_ended_at`, `run_duration_seconds` |
| Exit code; signal number when the command was killed by a signal | `run_exit_code`, `run_exit_signal` |
| Label | `run_label` |
| Git HEAD commit (or why it is unknown), dirty flag (or why unknown), branch | `run_git_commit` / `run_git_commit_error`, `run_git_dirty` / `run_git_status_error`, `run_git_branch` |
| Credential-free remote identity (`github.com/lab/repo`) | `run_repo_remote_url` |
| Working-copy git tree id (see below) | `run_git_worktree_tree` / `run_git_worktree_tree_error` |
| Lockfile environment fingerprint (the one `lt repo` computes: `uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements.txt`, `pyproject.toml`, `environment.yml` at the checkout root, plus Python version and `LAB_TRACKER_CONTAINER_REF`) | `run_environment_hash`, `run_environment_files`, `run_environment_python`, `run_environment_container` |
| Declared output folders; files created or modified under them | `run_output_roots`, `run_output_count`, `run_outputs_truncated` |
| Run identity | `run_id` (`run-<UTC stamp>-<8 hex>`) |

Plus the usual capture keys (`watch_*`, `evidence_*`, capture host). The git
facts describe the code *when the command started*; outputs are diffed after
it ended. `run_git_commit` is also an exact-id key, so a run whose commit is
the `code_version` of exactly one committed analysis is proposed as derived
from it.

### Output folders

No folder is watched unless declared with `--output`. The checkout's
`watch.json` roots are deliberately not used as a default: `lt watch` already
captures new files there as their own notes, and snapshotting a large
acquisition folder before and after every command would add unbounded latency
to every run.

Each declared folder is snapshotted (size and mtime of every regular file)
right before the command starts and again right after it exits. A file that is
new is `created`; one whose size or mtime changed is `modified`; the number of
files that disappeared is reported. VCS folders (`.git`, `.hg`, `.svn`) and
`.lab-tracker/` are skipped and symlinked folders are not followed. Each
changed file becomes an artifact pointer -- `file://` URI, size, mtime, and a
`sha256:` content hash -- never a copy of its bytes:

- files larger than 64 MiB, or past 512 MiB hashed in one run, keep size and
  mtime only (no `content_hash`); so does a file that changed while it was
  being hashed;
- at most 200 changed files are listed (all are counted in
  `run_output_count`; `run_outputs_truncated` is set when the list is cut);
- a snapshot stops after 20,000 files across all output folders, and the note
  says the diff may be incomplete.

### Secrets

The command line is redacted before it is written anywhere (event file, note
body, or metadata):

- the value of a flag whose name looks secret: `--token X`, `--api-key=X`,
  `--password X`, `-password X`, `--secret`, `--auth`, `--cookie`,
  `--client-secret`, ... (the next argument is redacted unless it is itself a
  `--` option);
- `KEY=VALUE` arguments whose key looks secret (`env API_TOKEN=...`,
  `PGPASSWORD=...`, Hydra `db.password=...`);
- URL credentials (`https://user:pw@host` becomes `https://[REDACTED]@host`; an
  ssh login name is kept, its password is not) and secret-looking query values
  (`?token=`, `?sig=`, `?key=`, `X-Amz-Credential`, ...);
- `Authorization:`/`Cookie:`-style header values, `Bearer` tokens, and
  well-known token formats anywhere (GitHub `ghp_`/`github_pat_`, GitLab
  `glpat-`, Slack `xox*-`, AWS `AKIA`/`ASIA`, `sk-`, Google `AIza`, Hugging
  Face `hf_`, JWTs, PEM private keys).

Names that only point at a secret (`--password-file`, `--token-name`) and
negations (`--no-password`) are kept. The environment is never recorded, and
the remote is stored as a credential-free identity. Redaction is a best-effort
filter for the obvious cases: prefer passing secrets through the environment
or files rather than on the command line.

## Exit Codes, Signals, and Output

- `lt run` exits with the command's exit code. A command killed by signal N
  exits `128 + N` (143 for SIGTERM, 130 for SIGINT), as the shell reports it.
  A command that does not exist exits 127 and one that cannot be executed
  exits 126, each with one `lt run: <cmd>: ...` line on stderr; nothing is
  recorded for a command that never started.
- While the command runs, Ctrl-C and Ctrl-\ go to the command (the terminal
  delivers them to it) and `lt run` waits for it to finish; a SIGTERM or
  SIGHUP sent to `lt run` is forwarded to the command. Either way the run is
  still recorded with how it ended.
- `lt run` never writes to stdout, so pipelines are unaffected, and it prints
  at most one line to stderr per run: why a run was not captured (unbound
  project), a dropped `--session`, a capture failure, or a failed sync. A
  capture failure never changes the exit code.

## Sync and Review

The event is written to the checkout's watch outbox
(`<checkout>/.lab-tracker/outbox/watch`, or `LAB_TRACKER_WATCH_OUTBOX`); a run
outside any git checkout with `--project` or `LAB_TRACKER_PROJECT_ID` uses
`.lab-tracker/outbox/watch` under the working directory. When a server is
configured (`LAB_TRACKER_BASE_URL`, or a profile saved by `lt setup connect`),
`lt run` then drains that outbox best effort, like the `lt repo` hook, with a
10-second client timeout; unconfigured, it makes no network call and the run
waits for `lt outbox sync`, `lt watch run`, or the scheduled drain
(`lt setup schedule`). `lt outbox status` lists queued runs.

## Code Identity for Uncommitted Code

A capture that names only a commit cannot say which code produced it when the
working tree was dirty -- the common case while an analysis is being
developed. So the client also records the git **tree id of the working copy**:
the tree `git add -A && git commit` would record at that moment (tracked
changes plus untracked, non-ignored files; ignored files do not count). Two
equal tree ids are the same bytes of code. A clean checkout's worktree tree is
exactly `HEAD^{tree}`.

| Key | Stamped by | When |
| --- | --- | --- |
| `run_git_worktree_tree` | `run_context()` figure captures; `lt run` | when the run context opens / the command starts |
| `capture_git_worktree_tree` | plain figure and file captures (`savefig`, `capture_figures`, `capture`, autotrack) inside a git checkout | at save time, *without the saved file itself* |
| `hpc_git_worktree_tree` | `lt hpc begin` and `lt hpc finish` | when the event is written (a manifest's own value wins) |
| `repo_git_tree` | `lt repo` commit events | the commit's own tree (`git rev-parse <sha>^{tree}`), never the working copy |

How it is computed and bounded:

- In a scratch `GIT_INDEX_FILE` seeded from a copy of the real index and a
  scratch `GIT_OBJECT_DIRECTORY` that borrows the repository's objects as an
  alternate: `git add -A` then `git write-tree`. The user's index and
  `.git/objects` are never written, and `git status` runs with
  `GIT_OPTIONAL_LOCKS=0`.
- `.lab-tracker/` is always left out, as are `lt run --output` folders and the
  saved file of a figure capture (an output is not the code that produced it).
  A left-out tracked path keeps its indexed content.
- More than 5,000 changed or untracked paths, or more than 64 MiB in them,
  records `*_git_worktree_tree_error: too_large` instead of hashing a data
  dump. Gitignore data and output folders so they never count.
- One timeout covers the whole computation: `LAB_TRACKER_GIT_TIMEOUT_SECONDS`
  (default 10 s), capped at 2 s for plain figure/file captures so a save is
  never held up. A slow git records `timeout`; missing git records
  `git_unavailable`; any other failure records `failed`. A capture never fails
  because of it, and outside a git checkout nothing is recorded.
- Results are cached per process by a signature of HEAD, the exclusions, and
  the size/mtime/inode of every changed path, so repeated saves of the same
  figure from the same code reuse one computation.
- `LAB_TRACKER_WORKTREE_TREE=0` turns it off everywhere (`disabled`).

Limitation: an output saved *untracked and not ignored* inside the checkout
becomes part of every later worktree tree, so a figure saved after another one
names a tree no commit has. Keep outputs in gitignored folders or outside the
checkout for exact matches.

### Worktree-tree provenance proposals

Every batch execution (the same deterministic stage as the content-hash and
exact-id detectors) proposes a `was_derived_from` provenance link with
`basis: worktree_tree_match` from each note carrying
`capture_git_worktree_tree`, `run_git_worktree_tree`, or
`hpc_git_worktree_tree` (first key that resolves, in that order) to the
**earliest other** note in the same project whose `repo_git_tree` is the same
full tree id: the commit whose code produced the capture. That commit may come
before the capture (a clean checkout) or after it (the person committed what
they ran). Only full 40- or 64-hex tree ids match, never prefixes.

The link is only `PROPOSED`: a person accepts or rejects it on the review page
("same code tree as a commit"). A pair declined once is never re-proposed, and
the detector's failure never fails the batch or the other detectors. Accepted
note-to-note links render as `wasDerivedFrom` in PROV-O export (see
[provenance-export.md](provenance-export.md)).

## Kill Switches

- `lt run` is opt-in per command: drop the prefix and nothing is captured.
- `--no-drain` keeps runs local until the next sync.
- Remove `lt_ids.json` (or unset `LAB_TRACKER_PROJECT_ID`) to stop capture in
  a checkout; `lt run` then only prints why it did not capture.
- `LAB_TRACKER_WORKTREE_TREE=0` stops every worktree tree computation.

## Troubleshooting

- `lt run is not capturing runs in ...`: bind the checkout with
  `lt project bind`, set `LAB_TRACKER_PROJECT_ID`, or pass `--project`.
- `queued this run ... but could not sync it`: the run is safe in the outbox;
  fix connectivity or authentication and run `lt outbox sync`.
- `run_git_worktree_tree_error: too_large`: gitignore the data or output
  folders inside the checkout, or move them out of it.
- A run's exit code looks wrong in CI: `lt run` reports what the shell would
  (`128 + N` for signals); check `run_exit_signal` in the note.
