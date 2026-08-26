# Analysis Repo Capture

`lt repo` is the third offline-first capture adapter, alongside `lt watch` and
`lt hpc`. It records the *state of an analysis repository* — commit SHA, dirty
flag, remote, branch, produced artifacts, and an environment fingerprint — as
durable local outbox events that later sync into staged Lab Tracker evidence
notes. Like its siblings it never blocks the caller. Commit events upload a
bounded textual diff (800 lines by default) alongside metadata and hashes;
declared run artifacts stay as pointers. Everything semantic remains behind the
human review gate.

The usual flow: a post-commit git hook self-reports every commit; researchers
optionally annotate captures or declare run outputs; the outbox drains whenever
Lab Tracker is reachable.

## Setup

```bash
cd /path/to/analysis-repo
lt repo init --project <project-uuid> [--default-question <question-uuid>]
lt repo install-hook
```

`init` writes `.lab-tracker/repo.json` (gitignore `.lab-tracker/` — it is
host-local scratch). `install-hook` writes a managed block into the repo's
`post-commit` hook:

- The block lives between `BEGIN/END LAB TRACKER REPO HOOK` markers; reinstalls
  update it in place. A foreign hook is never clobbered: without `--force` the
  install refuses; with `--force` the block is inserted *before* the foreign
  body, so a trailing `exit`/`exec` there cannot disable capture, and since the
  block never exits, the foreign hook still runs.
- The repo config is resolved at install time and pinned into the hook, so a
  missing config fails loudly at install instead of silently on every commit.
- The hook body is POSIX sh, which git runs on every platform (Git for Windows
  bundles one) — no separate Windows installer is needed. Disable per-repo with
  `LAB_TRACKER_REPO_HOOK_ENABLED=0`; point at a different client with
  `LAB_TRACKER_LT`.
- A post-commit hook can never block the commit; on capture failure it prints a
  one-line warning and the commit proceeds.

### Optional repository conventions

If repository-specific names or layouts help interpret later review proposals,
explicitly enroll the relevant tracked text files:

```bash
lt agent-context status
lt agent-context add AGENTS.md --dry-run
lt agent-context add AGENTS.md --yes
```

Enrollment writes only `.lab-tracker/agent-context.json`. Each future commit
capture reads the enrolled files from that exact Git tree—not from dirty
working-tree edits—strips Lab Tracker's own managed prompt blocks, and attaches
a bounded, hashed snapshot to the staged note metadata. The commit hook still
does capture only; scheduled or explicit review remains responsible for proposal
generation.

Use `lt agent-context remove AGENTS.md --dry-run` followed by `--yes` to stop
including a file. Repository conventions are untrusted descriptive context, not
instructions or scientific evidence, and every proposed graph operation still
requires human review.

## Capture Modes

### Post-Commit Hook

Every `git commit` records a `commit` event: commit SHA, branch, remote, commit
subject/author, dirty-tree flag, environment fingerprint, file summary, and a
bounded textual diff. Events are idempotent per commit — a re-fired hook updates
nothing and creates no duplicates. The hook only lands the capture in the
staged-note inbox; graph proposal generation waits for the configured
daily-review schedule or an explicit on-demand review trigger.

### Annotation

Re-running `lt repo report` for an already-captured commit merges
argument-wise: explicit flags win, everything else keeps its captured value.

```bash
lt repo report --summary "Sweep over latency window" --question <uuid> --tag pilot
```

Annotating a *pending* capture updates it in place (`action: updated`); a bare
hook re-fire never reverts an annotation (`unchanged`); annotating an already
*synced* capture writes a new event so the staged note is never desynced
(`recaptured`).

### Run Outputs

Declare produced files at the end of a run — each is fingerprinted
(stability-checked sha256 + size; bytes stay put):

```bash
lt repo finish --artifact results/decoding.csv --artifact figures/summary.png \
  --summary "Decoded stimulus identity from held-out trials."
```

`finish` events are per-run: two runs at the same commit stay distinct. The
same `--artifact` flag works on `report`.

### Environment Fingerprint

Each capture hashes whichever declared lockfiles exist at the repo root
(`uv.lock`, `poetry.lock`, `Pipfile.lock`, `requirements.txt`,
`pyproject.toml`, `environment.yml`) together with the capturing interpreter
version and an optional `LAB_TRACKER_CONTAINER_REF`, into
`repo_environment_hash`. A lockfile change at the same commit refreshes the
pending capture. At curation the hash can populate
`Analysis.environment_hash`.

## Sync And Review

```bash
lt repo status                 # outbox summary (pending/failed/synced)
lt repo sync [--dry-run] [--request-draft] [--limit N]
```

Sync uploads each event as a staged markdown note (`provider=git`) under the
project's normal review flow. Commit notes contain their bounded diff; the inbox
exposes it through the safe text-asset preview, and scheduled review includes it
within the aggregate source-context budget. Events are deduplicated through the
evidence index. The
shared evidence identity is `<normalized-remote>@<commit>` — the same identity
`scripts/create-analysis-graph-draft.py` emits, so hook-based and CI-based
capture of one commit dedup to one identity rather than parallel note streams.

Under today's device-token allowlist the staged-note sink works with a device
token; graph-draft requests (`--request-draft`) and any future first-class
registration need a user or personal-access token.

## Curation: From Note To Provenance

`lab_tracker.repo_bridge` turns an accepted repo note's metadata into analysis
fields (pure helpers; nothing auto-commits):

- commit SHA → `Analysis.code_version` (commits are encoded as pins/version
  strings, per the project's provenance design — no commit-entity DAG),
- declared artifact pointers → `ExternalArtifactReference` metadata
  (`file://` URI + sha256). Direct host paths are not resolver authority; map
  the pointer to a registered local or Git store before requesting bytes,
- `repo_environment_hash` → `Analysis.environment_hash`.

To pin a specific *code file* verifiably, register the repository as a `git`
data store and use `repo_bridge.git_code_pin` to construct a portable repository
path plus a full lowercase, nonzero SHA-1 or SHA-256 object ID. Mutable refs,
abbreviations, revspecs, traversal, and platform-specific path aliases are
rejected rather than normalized. The GitResolver fetches the blob read-only and
verifies its hash while retaining the logical `store://` identity. Resolution is
gated by the strict structural `LAB_TRACKER_GIT_ALLOWED_REMOTES` policy
(deny-by-default), a protocol allowlist, a size cap, and a bounded fetch cache.
Grants match the scheme, normalized host, effective port, SSH user, URL/SCP path
style, and case-sensitive whole path segments—not a raw string prefix. Git's
effective URL must use the exact reconstructed canonical spelling before a
query or fetch, and HTTP redirects are disabled.

That policy approves a logical Git endpoint; it does not sandbox host-owned
transport configuration. The service operator must exclusively control Git
credential helpers, Git/HTTP proxy and TLS configuration, the SSH agent and
keys, and OpenSSH routing such as `HostName`, `ProxyJump`, or `ProxyCommand`.
Those facilities may route an approved name through another host, so do not
mount researcher-writable Git, credential, proxy, or SSH configuration into the
Lab Tracker service. Persisted roots and policy entries must never contain
credentials. See
[external-artifact-resolution-design.md](external-artifact-resolution-design.md).

## Boundaries

- **Commit-time vs run-time.** The hook captures what a commit *is*, including a
  bounded textual diff; `finish` captures what a run *produced* as metadata and
  hashes. Declared data files never leave the host.
- **Dirty working trees** are recorded as a flag on the capture, but there is
  no commit to pin — uncommitted state is metadata, not resolvable provenance.
- **No continuous monitoring.** Capture is event-based (commits, explicit
  reports, run finishes); Lab Tracker never clones or polls repositories.

## Troubleshooting

- `lt repo install-hook` fails with "No repo config found": run `lt repo init`
  in the repository first.
- Existing non-managed hook: re-run with `--force` (the managed block is placed
  ahead of it); a hook with a non-sh shebang (e.g. python) is refused — chain
  `lt repo report` from it manually.
- Commits recorded but never syncing: run `lt repo status`, then
  `lt repo sync` on a machine that can reach Lab Tracker.
