# Notebook and Script Capture

Most analysis figures never pass through an explicit capture call. In Jupyter
they are displayed inline and never saved; notebooks themselves are saved
every couple of minutes by autosave; and plain `python script.py` runs never
load an IPython startup file. Three optional, consent-gated hooks close those
gaps. After setup there is nothing to do per figure or per save: captures
land as **staged notes** for normal human review, and nothing is committed to
the graph, linked, or drafted on the scientist's behalf.

All three follow the rules of the original figure autotrack:

- **Bound project only.** A capture is filed only when its project comes from
  `autotrack(project_id=...)`, `LAB_TRACKER_PROJECT_ID`, or the checkout's
  `lt_ids.json` (`lt project bind`). Anything else is skipped: nothing is sent
  or queued, and one stderr notice names each unbound checkout (or, outside a
  git checkout, the folder).
- **Kill switch.** `LAB_TRACKER_AUTOTRACK=0` (also `false`, `no`, `off`) turns
  every hook off, including the scripts `.pth`, which checks it at interpreter
  start.
- **Fail-soft.** No hook raises into a notebook, a save, or a script, or
  changes an exit code; each cause is reported at most once on stderr. An
  unreachable server queues the capture in the checkout's watch outbox
  (`.lab-tracker/outbox/watch`), which `lt watch run` or `lt outbox sync`
  drains later.

## Setup

| Piece | Command | Writes |
| --- | --- | --- |
| IPython startup (saves and inline displays) | `lt setup autotrack --yes` | `~/.ipython/profile_default/startup/50-lab-tracker-autotrack.py` |
| Jupyter save hook (daily notebook pages) | `lt setup autotrack --jupyter --yes` | `~/.jupyter/jupyter_server_config.d/lab-tracker.json` |
| Plain scripts | `python -m lab_tracker_client setup autotrack --scripts --yes` | `lab_tracker_autotrack.pth` in that Python's site-packages |

Every form takes `--dry-run` (show the change, write nothing) and
`--uninstall` (remove the managed file); a run without `--yes` or `--dry-run`
fails. Without `--jupyter` or `--scripts`, `lt setup autotrack` manages only
the IPython startup file, exactly as before; with them it manages only the
named pieces. `lt setup status` reports all three under `autotrack`
(`installed`, `up_to_date`, and for the new pieces `jupyter` and `scripts`).

Two installation details matter:

- The Jupyter hook runs inside the Jupyter server, so `lab-tracker` must be
  installed in the environment that runs `jupyter lab` / `jupyter notebook`,
  and the server must be restarted once. `JUPYTER_CONFIG_DIR` moves the config
  folder.
- The `.pth` file affects only the Python environment it is written into.
  `lt` installed with `uv tool install` runs in its own tool environment, so
  run the scripts setup with the interpreter your analysis scripts use, for
  example `.venv/bin/python -m lab_tracker_client setup autotrack --scripts
  --yes` or `uv run lt setup autotrack --scripts --yes` inside the analysis
  project. The payload names the `python` and `site_packages` it targeted.

## Inline notebook figures

With the IPython startup file installed, `autotrack()` inside IPython or a
Jupyter kernel also captures the figures a cell **displays**. Every display
path (matplotlib-inline's end-of-cell `flush_figures`, `display(fig)`, a
figure as a cell's last value) asks the shell's `display_formatter.format` for
the figure's image, so the hook wraps that one call and keeps the exact PNG (or
JPEG) bytes the kernel sent to the notebook; nothing is rendered twice.

- **Coalesced per cell.** Displays are collected while the cell runs and
  captured once it has finished (`post_run_cell`, which IPython fires after
  `flush_figures`). A figure displayed several times in one cell is captured
  once, with its last bytes; a figure the same cell saved to a matching file is
  left to the save's own capture.
- **Re-running a cell coalesces.** The capture's logical id is
  `display/<notebook>/<cell>/figure-<n>`: the notebook path (checkout-relative
  when possible), the cell's Jupyter cell id when the frontend sends one (else
  the SHA-256 of its source), and the figure's order in the cell. A re-run
  lands on the same staged note; changed bytes mark it
  `figure_review_bytes_stale`, exactly like re-saving a file to the same path.
  (The server refuses a capture-id replay whose fields differ, and every
  capture has a new observed-at time, so the client looks up the note that
  capture id made, first in a per-process cache, then among the project's
  notes, at most 5,000, and coalesces into it.)
- **Notebook path.** From `JPY_SESSION_NAME` (set by Jupyter Server), else VS
  Code's `__vsc_ipynb_file__`, else `unknown` (the kernel's folder anchors the
  project binding).
- **Metadata.** `figure_display_captured=True`, `figure_autotracked=True`,
  `figure_display_format`, `notebook_path`, `notebook_path_source`,
  `notebook_cell_execution_count`, `notebook_cell_source_sha256`,
  `notebook_cell_id` (when known), and `notebook_figure_index`, plus the usual
  evidence, host, run, and session keys. The source URI is the notebook's
  `file://` URI with a `#display=<cell>/figure-<n>` fragment.
- **No files on a live capture.** The bytes go straight to the server. Only
  when the server is unreachable are they written, once per distinct content,
  to the outbox's private `blobs/` folder so the queued event can deliver them.

Not captured: SVG-only or PDF-only inline formats (the server refuses SVG),
interactive widget backends (`ipympl`), and GUI windows from a terminal
IPython session (saves are still captured there).

## Notebook saves as daily pages

The Jupyter save hook (`lab_tracker_client.notebook_capture.post_save_hook`)
turns a day of saves of one notebook into **one** staged note, a lab-notebook
page. For a `.ipynb` saved in a bound checkout it writes a staged-note event
to the checkout's watch outbox whose markdown body holds:

- a pointer: the checkout-relative path, `file://` URI, SHA-256 and size of the
  notebook (the file itself is never uploaded; the event names it as an
  artifact pointer);
- the kernel name and language, and cell counts (code, executed, markdown,
  raw, image outputs, errors);
- the text of the markdown cells, capped at **20,000 characters** per page,
  with credentials, query strings, and fragments stripped from URLs and
  embedded `data:` images omitted;
- one summary line per code cell (at most **200** cells, each line at most
  200 characters): execution count, line count, imported modules, defined
  names, and output kinds (error class names only). Code is never copied.

Notebooks larger than 25 MB are hashed but not parsed, so an autosave never
stalls the server; their page is the pointer alone. Note metadata carries
`notebook_path`, `notebook_sha256`, `notebook_size_bytes`, `notebook_kernel`,
`notebook_language`, `notebook_cell_count`, `notebook_code_cell_count`,
`notebook_markdown_cell_count`, `notebook_markdown_truncated`,
`notebook_local_day`, `notebook_saved_at`, and `notebook_page=True`; the
evidence capture kind is `notebook` and the provider `jupyter-notebook`.

**Coalescing.** There is at most one pending page per notebook per local day.
Each save replaces the pending page (a save with unchanged bytes writes
nothing). The page carries `payload.deliver_after`, the next local midnight,
and the watch sync (`lt watch run`, `lt watch sync`, `lt outbox sync`) leaves
it pending, reported as skipped with reason `not_due`, until then; so the note
that lands is the day's last saved state. Once a page is synced, later saves
from that same day add nothing; a save on a later day starts a new page. The
day's page is therefore reviewed the next day, typically in the scheduled
daily review. Pages for notebooks outside a git checkout (bound only by
`LAB_TRACKER_PROJECT_ID`) queue in that folder's own `.lab-tracker/outbox`
and drain when `lt outbox sync` runs there.

**How it is enabled.** Jupyter Server reads `jupyter_server_config.d/*.json`
only to enable server extensions, so the managed file enables
`lab_tracker_client.notebook_capture` as an extension, and the extension
registers the hook with `register_post_save_hook`, next to any
`post_save_hook` already configured rather than in its place. The setup
payload lists other configured `post_save_hook` values it found
(`other_post_save_hooks`) and leaves them untouched; a `lab-tracker.json` that
Lab Tracker did not write is refused, never overwritten. On a server too old to
have `register_post_save_hook`, the extension fills an empty `post_save_hook`
slot and otherwise refuses with a log warning. To wire it by hand instead, set
`c.FileContentsManager.post_save_hook =
"lab_tracker_client.notebook_capture.post_save_hook"`.

Not captured: notebooks saved by an editor that bypasses Jupyter Server, such
as VS Code's native notebook editor (its inline figures are still captured by
the kernel hook).

## Plain scripts

`lt setup autotrack --scripts` writes `lab_tracker_autotrack.pth` into the
current environment's site-packages. Python runs its one `import` line at
every interpreter start, so the line is kept nearly free (about half a
millisecond): it reads `LAB_TRACKER_AUTOTRACK` and, when the packaged
bootstrap file still exists, runs the stdlib-only
`lab_tracker_client/_autotrack_pth.py` from its cached bytecode, without
importing `lab_tracker_client`; any error is swallowed. The bootstrap only adds
a small `sys.meta_path` watcher. When `matplotlib.figure` or
`matplotlib.pyplot` finishes importing in a process that is not IPython, the
watcher swaps in lazy stand-ins for `Figure.savefig` and `pyplot.show` and
removes itself. The first save or show imports Lab Tracker and installs:

- the autotrack **savefig hook**: saves to a path, or to an open real file
  whose `name` is its filesystem path (the file is flushed first); in-memory
  buffers such as `BytesIO` are ignored, as are suffixes outside the figure
  patterns;
- a **`plt.show()` hook** for scripts that never save: before a blocking
  show, each open figure is rendered as PNG and captured with logical id
  `show/<script>/run-<run id>/figure-<number>`, at most once per figure
  number per run, so each figure is one note per run; a figure the run saved
  to a matching file is left to that save. Animation frames are not
  captured: `plt.pause()` calls `show(block=False)` on every frame, and a
  bare `show()` in interactive mode (`plt.ion()`) returns at once, so both
  are skipped. The project binding follows the script's checkout (or the
  working folder for `python -c`). Metadata: `figure_show_captured=True`,
  `figure_number`, `script_path`, `script_run_id`.

A script that imports matplotlib but never saves or shows a figure never
imports Lab Tracker. `python -S` or `-I` skip `.pth` processing entirely.
Because every script run is a new process, the unbound-checkout notice is
remembered in `autotrack-notices.json` in the client config folder
(`LAB_TRACKER_CONFIG_DIR`, default `~/.lab-tracker`): each checkout or loose
folder is named at most once a week, and the file keeps at most 256 entries.
The `.pth` file is pure ASCII (a non-ASCII path is escaped), since Python
3.10-3.12 read `.pth` files in the locale encoding.

## Limits and kill switches

- `LAB_TRACKER_AUTOTRACK=0` disables all of the above;
  `LAB_TRACKER_CAPTURE_OUTBOX=0` stops figure captures from queueing offline.
- `lt setup autotrack --uninstall`, `--jupyter --uninstall`, and
  `--scripts --uninstall` remove each managed file.
- Captures are pointers plus bounded excerpts: figure previews follow the
  existing 2 MB preview cap, notebook pages the caps above.
