# External Harness Drafting — Security Status & Operator Responsibilities

> **Status: NOT a completed enablement gate.** This is a design-intent + operator-
> responsibilities memo with known gaps, corrected after an independent
> adversarial review (2026-07-08). `graph_draft_provider=external_harness` ships
> **default-off and must stay off** until the open gaps below are closed and a
> real security review is run against the shipped runtime path — not against
> in-process test doubles.

This covers `graph_draft_provider=external_harness`. It remains default-off. A run
fails closed unless **all** of these are set, **and** an operator sandbox wrapper
command is configured (see the corrected fail-closed row):

- `LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_ENABLED=true`
- `LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_SANDBOX_PROFILE=operator_managed`
- `LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_EGRESS_PROFILE=vendor_api_only`
- `LAB_TRACKER_GRAPH_DRAFT_EXTERNAL_HARNESS_COMMAND=<a real sandbox/egress wrapper>`

## What is genuinely enforced in code

- **Propose-only / human commits.** The harness has no Lab Tracker credential and
  runs as a non-interactive principal; `require_interactive` fail-closes accept,
  bulk-accept, and commit. Output funnels through the unchanged
  `create_batch_graph_draft` → validate → `operations_from_graph_patch` path and
  lands `READY` change sets with every op `PROPOSED`. A compromised harness cannot
  commit.
- **No server secrets in the child.** `SubprocessHarnessDraftRunner` builds a
  scrubbed environment (OS basics + the vendor API-key allowlist only) and raises
  if any `LAB_TRACKER_*`/`DATABASE_URL` leaks in. Pinned by
  `tests/test_graph_drafts.py::test_external_harness_subprocess_runner_scrubs_lab_tracker_env`.
- **No general artifact-deref / write tools on the MCP surface.** The MCP tool
  set is the 18 allowlisted graph reads plus `submit_graph_patch` by default;
  `resolve_artifact` and all `create_*` tools are structurally absent. If
  `LAB_TRACKER_GRAPH_DRAFT_GITHUB_READ_ENABLED=true`, three additional reads
  list project-effective GitHub data stores and list/read bounded UTF-8 files at
  a full commit hash. Store scope is enforced server-side and the GitHub token
  never reaches the child.
- **Fail-closed at the default and without a wrapper.** The runner refuses to
  launch unless both profiles are set to their "on" values **and** an operator
  wrapper command is supplied; it will not spawn a bare vendor binary while
  attesting `operator_managed`.
- **Bounded resources.** stdio is backed by on-disk temp files (no in-RAM pipe
  buffering → no worker OOM), stdout is truncated at `max_stdout_bytes`, the run
  has a wall-clock timeout, and the whole process tree is killed on timeout or
  overflow. Only bounded trace metadata is persisted; raw stdout/stderr are not.

## Known gaps — these are NOT enforced by application code (must not be trusted)

| Claim previously made | Reality (2026-07-08 review) |
| --- | --- |
| "Cross-project isolation — dispatches every read through `ScopedGraphDraftReadToolExecutor.execute`." | **False at runtime.** The per-run MCP server (`build_fastmcp_server`) is **not served to the subprocess**; the child receives the pre-scoped batch context as **static data** and performs **no live scoped reads**. Cross-project leakage is prevented only *incidentally* (the child cannot read live at all). The cited test exercises an in-process double, not the shipped path. |
| "Omit sensitivity — sensitive note bodies are omitted from the model-facing payload." | The context builder previously treated `omit` identically to `redact`, leaking a sensitive note's raw content plus its raw-asset filename/checksum/storage-id/size. **Fixed:** the `omit` branch (`graph_draft_context.py` + the executor) now drops `raw_asset` **and** strips the `source_file_*` identifier keys the upload path mirrors into `metadata` (`omit_safe_metadata`), so filename/checksum/size no longer reach the model. Non-identifier metadata (e.g. `note_type`, the `sensitivity` tag itself) is intentionally retained as classification. This holds in the static context path; a live-read chokepoint for the harness is still not wired. |
| "OS isolation / native-tool denial / egress allowlist." | **Not code-enforced.** Native-tool denies and the egress host list are advisory JSON in the prompt only; there is no `--disallowedTools`, no OS network restriction, and no isolation primitive established by the app. These are entirely the operator's sandbox wrapper's responsibility. The persisted trace now records `app_code_enforced: false` for both. |
| "Sandbox/egress fail closed." | The default fails closed, and the runner now additionally refuses to launch without an operator wrapper command. But the app still cannot *verify* that the wrapper actually isolates anything — a wrapper that merely `exec`s the bare binary would pass. |

## Operator responsibilities (before ever enabling)

The `operator_managed` / `vendor_api_only` profiles are a **deployment promise the
app cannot check**. Before setting the enable + profile variables, the operator
must supply `..._COMMAND` as a launcher that, outside the app, actually:

- runs the vendor CLI under a low-privilege account / Windows Sandbox / WSL2
  namespace that **denies** read access to the repo, `.env`, `lab_tracker.db`, and
  server keys, in a throwaway working directory;
- restricts outbound network to the vendor API host(s) only; and
- disables the harness's own native Bash/Read/Write/WebFetch tools.

Optional GitHub repository access does not change the child egress policy:
Lab Tracker's server performs the API call through the scoped executor. The
operator must use a fine-grained GitHub token limited to the registered
repositories with Contents read-only access, keep the option disabled when it
is unnecessary, and treat the registered project/group `git` data stores as
the repository allowlist. A single-user workstation may use the explicit
`credential_ref=gh-cli:github.com` keyring reference instead of storing a token
in Lab Tracker configuration; no other credential-reference schemes are
resolved by this feature.

Without that wrapper, enabling the feature spawns an **unisolated** vendor CLI with
full host network and same-user filesystem access. Do not enable until these
controls are verified independently and a real security review is run against the
runtime path.
