# MATLAB figure capture

MATLAB users can capture analysis figures into Lab Tracker without installing
or calling Python. The MATLAB package talks directly to the Lab Tracker HTTP API
and uses the same retained workflow as the Python client: saved figures become
**staged evidence notes** with source URI, content hash, and idempotency
metadata. Scientific meaning still comes later through human graph-draft review.

## Install

From a checkout of this repository, add the MATLAB package folder to your path:

```matlab
addpath("/path/to/lab-tracker/matlab")
```

The package is namespaced as `labtracker`, so it will not shadow MATLAB's built
in `savefig` unless you call `labtracker.savefig(...)`.

## Configure

Use the same environment variables as the Python client:

```bash
export LAB_TRACKER_BASE_URL=http://127.0.0.1:8000
export LAB_TRACKER_PROJECT_ID=<PROJECT_UUID>
export LAB_TRACKER_ACCESS_TOKEN=<TOKEN>
```

If your local server has authentication disabled, `LAB_TRACKER_ACCESS_TOKEN` is
not required. Prefer setting `LAB_TRACKER_ACCESS_TOKEN` to a personal access
token when auth is enabled. As a deprecated fallback, when no access token is set
the MATLAB client can log in with `LAB_TRACKER_USERNAME` and
`LAB_TRACKER_PASSWORD`.

You can also configure a client explicitly:

```matlab
client = labtracker.Client( ...
    "BaseUrl", "http://127.0.0.1:8000", ...
    "AccessToken", "<TOKEN>", ...
    "ProjectId", "<PROJECT_UUID>");
```

## Capture a figure

Replace a plain figure export with `labtracker.savefig`:

```matlab
x = linspace(0, 2*pi, 200);
fig = figure;
plot(x, sin(x));

result = labtracker.savefig(fig, "figures/sine-summary.png", ...
    "Metadata", struct("analysis_name", "sine-smoke"));
disp(result.action)
```

`result.action` is usually `imported`. If a retry reuses an existing
`client_capture_id`, the server may return the existing note and the action is
`coalesced`. The wrapper is fail-soft: it always saves the local figure and
never errors into your script. When the server cannot be reached the action
is `queued` (see [Offline queue](#offline-queue)); when no project is known
it is `skipped`; any other failure is `failed`. Each cause is warned about
once per MATLAB session (`warning` identifiers `labtracker:unconfigured`,
`labtracker:queued`, `labtracker:circuitOpen`, `labtracker:captureFailed`,
`labtracker:sessionRefused`, `labtracker:queueFailed`), so a loop that saves
a hundred figures while offline prints one line.

To capture an already-saved file:

```matlab
client = labtracker.Client.fromEnv();
result = labtracker.uploadFigure("figures/sine-summary.png", "Client", client);
```

Each figure note records metadata such as:

- `evidence_source_provider = "local-figure"`
- `evidence_source_uri = "file://..."`
- `evidence_content_hash = "<sha256>"`
- `evidence_adapter = "lab-tracker-matlab-figure"`
- `figure_client_capture_id = "figure:<logical path>"`

Large files are not uploaded wholesale by default. If the figure exceeds
`PreviewMaxBytes` (2 MB by default), the MATLAB package uploads a small pointer
note with the original file URI, content hash, and size, leaving the full figure
in your analysis folder.

## Project, session, and run context

The MATLAB package resolves the same context the Python client does, from the
same files, so a MATLAB figure lands next to the Python captures of the same
analysis:

- **Project:** the `ProjectId` argument, else the client's project
  (`LAB_TRACKER_PROJECT_ID`), else the `lt_ids.json` of the git checkout the
  figure is saved in (written by `lt project bind`), else that checkout's
  `.lab-tracker/watch.json` project. With none of these the capture is
  `skipped` with reason `unconfigured`.
- **Session:** `LAB_TRACKER_SESSION_ID` (a session UUID or its `LT-` link
  code) for this MATLAB process, else the session `lt session use` recorded in
  `<checkout>/.lab-tracker/session.json` (`LAB_TRACKER_SESSION_CONTEXT` names
  another file) until its `expires_at`. The checkout is the one MATLAB's
  current folder is in. The note records `capture_session_id`,
  `capture_session_link_code` and `capture_session_source`; the session is
  also declared as a note target (labelled
  `declared_target_source = config_default`) when it came from
  `LAB_TRACKER_SESSION_ID`, or from a checkout context recorded for the
  capture's own project. If the server refuses that target (a session in
  another project, or one that does not exist), the upload is retried once
  without it and the session stays plain metadata.
- **Run facts** (the Python client's `run_*` keys), from `git` run with
  `system()` in MATLAB's current folder: `run_captured_at`, `run_git_commit`
  (or `run_git_commit_error` when git could not say), `run_git_dirty` (or
  `run_git_status_error`: an unknown state is never reported as clean) and
  `run_repo_remote_url`, the `origin` remote with any user, token, query or
  fragment removed (`https://user:token@github.com/Lab/Repo.git` becomes
  `github.com/lab/repo`). Pass `'RunMetadata', false` to leave them out. These
  probes have no timeout of their own, unlike the Python client's
  (`LAB_TRACKER_GIT_TIMEOUT_SECONDS`).
- **Host:** `capture_host_label` (`LAB_TRACKER_CAPTURE_HOST`, else the
  hostname) and `capture_platform`. The Python client's per-install id and
  client release are not stamped: they describe a Python install.

## Offline queue

When an upload fails because the server never answered (connection refused,
DNS failure, timeout), the capture is written as an event into the checkout's
watch outbox, `<checkout>/.lab-tracker/outbox/watch/` (or
`LAB_TRACKER_WATCH_OUTBOX`, or the watch config's `outbox`), and
`result.action` is `queued` with `result.queued_event` naming the file. Deliver
it later from a shell with the Python client:

```bash
lt outbox sync        # or the scheduled `lt watch run` (lt setup schedule)
```

The event has the same schema as a figure the Python client queued (source
URI and path, SHA-256 content hash from `java.security.MessageDigest`, size,
capture id, project, session, metadata and host), is written through a temp
file and a rename so a sync never reads half an event, and is named the way
`lt` names its own events, so a second save of the same bytes reuses it. The
sync uploads the figure file itself as a staged note, exactly as for a
Python-queued figure. The one difference: MATLAB cannot reproduce Python's
floating-point file mtime bit for bit, so its events record none, and the sync
checks the content hash and size alone before uploading (a figure changed or
deleted since the save is marked stale, not uploaded).

After a transport failure the package pauses uploads to that server for 30
seconds: saves in that window are queued at once instead of each waiting on
another connection timeout (`LAB_TRACKER_HTTP_TIMEOUT`, 15 s by default).
Set `LAB_TRACKER_CAPTURE_OUTBOX=0` to turn queueing off; an unreachable server
is then a `failed` capture.

## Smoke example

Run the included smoke script after setting the environment variables above:

```matlab
run("/path/to/lab-tracker/matlab/examples/capture_figure_smoke.m")
```

The script generates a small plot, saves it to your temp directory, and stages a
Lab Tracker evidence note. It uses only MATLAB APIs.

## Runtime smoke validation

The MATLAB package has source-contract tests in CI, but an actual end-to-end
capture can only be validated with a **licensed MATLAB runtime**, which the CI
runners do not have. There are two ways to run the real thing:

**Automated runner** — `scripts/matlab-smoke.sh` boots a disposable
auth-disabled Lab Tracker server on a temp SQLite database, creates a project,
runs `capture_figure_smoke.m` against it, and asserts the capture actually
imported (`result.action` is `imported` or `coalesced`). It **skips green** when
`matlab` is not on `PATH`, so it is safe to invoke anywhere:

```sh
scripts/matlab-smoke.sh    # no-op exit 0 without MATLAB; full smoke with it
```

**Manual runbook (token-authenticated, against any server)** — on a machine with
MATLAB, point at a running Lab Tracker and confirm a success action:

1. Start (or reach) a Lab Tracker server and note its base URL.
2. Create a user + project, then issue an access token (login, or a personal
   access token via `POST /auth/tokens`).
3. Export the client configuration:

   ```sh
   export LAB_TRACKER_BASE_URL="https://lab.example.org"
   export LAB_TRACKER_ACCESS_TOKEN="lpat_…"      # or username/password vars
   export LAB_TRACKER_PROJECT_ID="…"
   ```

4. Run the example and confirm the printed `result` has
   `action: "imported"` (first capture) or `action: "coalesced"` (a repeat of an
   identical figure):

   ```sh
   matlab -batch "run('matlab/examples/capture_figure_smoke.m')"
   ```

Because the MATLAB client is fail-soft — a misconfigured capture returns
`action: "skipped"` or `"failed"` rather than erroring — always check
`result.action`; a green exit alone does not prove a figure was captured.

**GNU Octave (no MATLAB license).** The HTTP client (`labtracker.Client`,
built on `matlab.net.http`) and `exportgraphics` need MATLAB, but every
`+labtracker/+internal` helper uses only `javaMethod`/`javaObject`, char
arrays and `jsondecode`/`jsonencode`, so the context and offline-queue code
also runs under Octave 7 or later. Where `octave-cli` is installed,
`tests/test_matlab_offline_queue.py` runs `labtracker.uploadFigure` under
Octave with a stand-in client whose upload fails the way MATLAB's does when
the server is down, then checks that Python reads, names and syncs the queued
event, and that Octave and Python resolve the same project and session. CI
runners without Octave skip those two tests; the hand-written event fixture
(`tests/fixtures/matlab/offline_figure_event.json`) is synced through
`lt outbox sync` everywhere. The live HTTP path is still only covered by the
licensed-MATLAB smoke above.

## Scope

The MATLAB package covers figure capture, raw figure-file upload, the offline
queue, and the project, session and run context above. Remaining gaps against
the Python client:

- no autotrack: MATLAB's own `saveas`/`exportgraphics`/`print` are not
  wrapped, so a figure is captured only through `labtracker.savefig` or
  `labtracker.uploadFigure` (or by shelling out to `lt capture file`);
- no generic `capture(root, patterns, kind)` for non-figure files, and no
  `run_context` (`run_id`, code file and line);
- queued events are delivered by the Python CLI (`lt outbox sync` or
  `lt watch run`); MATLAB does not drain the outbox itself;
- the connection profile saved by `lt setup connect` is not read; configure
  MATLAB with `LAB_TRACKER_*` variables or an explicit client;
- git probes are not bounded by a timeout.

The broader consumer automations (`lt watch`, `lt hpc`, and `lt export`) remain
Python CLI workflows. MATLAB scripts can still write files or manifests for
those tools to pick up, but the MATLAB package itself does not run a folder
watcher or scheduler adapter.
