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
not required. When auth is enabled and no access token is set, the MATLAB client
can log in with `LAB_TRACKER_USERNAME` and `LAB_TRACKER_PASSWORD`.

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
`coalesced`. If configuration or connectivity is missing, the wrapper is
fail-soft: it saves the local figure and returns `skipped` or `failed`.

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

## Smoke example

Run the included smoke script after setting the environment variables above:

```matlab
run("/path/to/lab-tracker/matlab/examples/capture_figure_smoke.m")
```

The script generates a small plot, saves it to your temp directory, and stages a
Lab Tracker evidence note. It uses only MATLAB APIs.

## Scope

The MATLAB package currently covers figure capture and raw figure-file upload.
The broader consumer automations (`lt watch`, `lt hpc`, and `lt export`) remain
Python CLI workflows. MATLAB scripts can still write files or manifests for
those tools to pick up, but the MATLAB package itself does not run a folder
watcher or scheduler adapter.
