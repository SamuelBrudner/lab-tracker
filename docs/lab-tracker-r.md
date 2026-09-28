# R figure capture

R users get the same low-effort figure capture Python notebooks have: once
autotrack is set up, a figure saved with `ggplot2::ggsave()` or through the
`png()`, `jpeg()`, `tiff()`, `bmp()` or `pdf()` file devices becomes a
**staged evidence note** with the file's URI, content hash, host, the active
session and a stable capture id, without changing the script. Nothing is
committed: a person still reviews the note and any graph draft made from it.

The R side does not talk HTTP. Each capture runs the Python client's
language-neutral entry point in the background:

```bash
lt capture file <path> --kind figure --require-bound \
  --metadata figure_autotracked=true --metadata capture_language=R \
  --metadata figure_r_device=<png|jpeg|tiff|bmp|pdf|ggsave>
```

so R captures share the Python client's configuration (`lt setup connect`,
`LAB_TRACKER_*`), offline queue, session handling and bound-project rule.
See [watch-folder-capture.md](watch-folder-capture.md#capturing-one-saved-file-from-any-runtime)
for the `lt capture file` contract.

## Setup (one consented command)

`lt` must be installed (see [setup.md](setup.md)). Then preview and apply:

```bash
lt setup autotrack --r --dry-run   # shows the diff for your R profile
lt setup autotrack --r --yes       # adds the managed block
lt setup autotrack --r --uninstall --yes   # removes it, leaving the rest
```

The block goes into the file R reads as the user profile:
`R_PROFILE_USER` when set, otherwise `~/.Rprofile` (on Windows, `R_USER` or
`HOME`, else the Documents folder). It is delimited by
`# --- BEGIN LAB TRACKER AUTOTRACK ...` / `# --- END LAB TRACKER AUTOTRACK ---`
lines; everything outside them is left as it was, and a damaged pair of
markers is refused rather than guessed around. A profile that is a symlink
(for example into a dotfiles repository) is edited at the file it points to,
which the command reports as `rprofile_target`; the link itself is never
replaced or deleted, and a link that points nowhere is refused. A `.Rprofile` in the folder R
starts in replaces the user profile for that session (an RStudio project's
own `.Rprofile`, for example); add `source("~/.Rprofile")` to it if you want
both. `lt setup status` reports the block (`autotrack_r`) and whether it is
current; rerun `lt setup autotrack --r --yes` after moving or reinstalling
`lt` and it refreshes the block in place.

### How the block loads the R code (distribution)

The R implementation is one base-R file, the `labtracker` R package in
[`r/labtracker`](../r/labtracker). The Python package ships a byte-identical
copy (`lab_tracker_client/r_labtracker/labtracker.R`), so installing or
upgrading `lt` installs or upgrades the R side; no CRAN or R install step is
needed. When R starts, the block:

1. does nothing when `LAB_TRACKER_AUTOTRACK` is `0`, `false`, `no` or `off`;
2. waits until `grDevices` is attached (R attaches default packages after the
   profile runs);
3. calls `labtracker::autotrack()` if the R package is installed
   (`R CMD INSTALL r/labtracker`), otherwise sources the copy shipped with
   `lt` into a private environment and attaches its four functions as
   `tools:labtracker`;
4. passes the `lt` that ran the setup, used after `LAB_TRACKER_LT` and before
   `lt` on `PATH` (R GUIs often start with a shorter `PATH` than your shell).

A load failure never stops R from starting; if the shipped copy has gone
missing (the Python environment moved), the block prints one message saying
to rerun the setup command.

### Without the profile block

```r
labtracker::autotrack()                       # after R CMD INSTALL r/labtracker
labtracker::autotrack(project_id = "<uuid>")  # bind every capture explicitly
labtracker::autotrack(metadata = list(rig = "2"))
labtracker::autotrack(FALSE)                  # remove every hook
labtracker::capture_file("figures/summary.png", wait = TRUE)  # one explicit capture
labtracker::autotrack_status()
```

## What is captured

- `ggplot2::ggsave()` to a `.png`, `.jpg`/`.jpeg`, `.tif`/`.tiff`, `.bmp` or
  `.pdf` file, captured once after it returns (its own device is not captured
  a second time). ggsave is wrapped in ggplot2's namespace, keeping its
  arguments, and re-wrapped whenever ggplot2 is loaded again.
- `png()`, `jpeg()`, `tiff()`, `bmp()` and `pdf()` opened with a file name,
  captured when `dev.off()` (or `graphics.off()`) closes the device. A
  page-numbered name (`"plot%03d.png"`) captures each page written since the
  device opened.

Not captured: `svg()` and `.svg` files (the server refuses SVG uploads), a
device opened without a file name (R's implicit `Rplots.pdf` / `Rplot%03d.png`,
which are not explicit saves), devices from other packages (`ragg`,
`svglite`, `Cairo`) except through `ggsave()`, device functions a package
imported before autotrack ran, and chunk images while knitr renders a
document (a `ggsave()` inside the document is still captured).

## Which saves, which project

Because the profile block runs in every R session, a save is captured only
when its project is *bound*: `autotrack(project_id = ...)`,
`LAB_TRACKER_PROJECT_ID`, or the `lt_ids.json` of the git checkout the file
is saved in (`lt project bind`). Any other save is skipped: nothing is sent or
queued, and one message names the unbound checkout (or folder) per session.
A figure never lands in a default project it was not meant for.

## Per-event effort, failure and limits

- **Effort after setup:** none. Saves return immediately; `lt` runs in the
  background (roughly a second of Python start-up per file, off your
  session's clock).
- **Fail-soft:** nothing raises into your session or changes a save's
  result. A missing `lt`, an unbound checkout, an unconfigured client, an
  unreachable server (the capture is queued in the checkout's
  `.lab-tracker/outbox/watch` for `lt outbox sync`) or a failed upload is
  reported with `message()`, once per distinct cause per R session, after the
  next top-level command. Results of a capture that has not finished within
  five minutes are dropped with one message.
- **Pointer, not copy:** files up to 2 MB are uploaded as the note's preview;
  larger ones become a pointer note with URI, hash and size, as in Python.
- **Script exit:** a capture started just before `Rscript` exits still
  completes in its background process; its notices are not shown.
- **Windows:** the hooks and block are written for Windows too (`system2`
  quoting, R's Windows home folder) but have only been exercised on Linux;
  the R test suite's stand-in `lt` is a POSIX shell script, so the R tests
  skip on Windows.

## Kill switches

- `LAB_TRACKER_AUTOTRACK=0` turns R autotrack off at startup and for any
  later save in a running session.
- `labtracker::autotrack(FALSE)` removes the hooks from the current session.
- `lt setup autotrack --r --uninstall --yes` removes the profile block.
- `LAB_TRACKER_CAPTURE_OUTBOX=0` stops offline queueing (unreachable saves
  then report a failure instead).

## Tests

`tests/test_r_autotrack.py` covers the profile block (install, update,
uninstall, dry-run, consent, damaged markers) everywhere. Where `Rscript` is
installed it also starts real R sessions against the installed block (with a
stand-in `lt` and with the real one) and runs the package's testthat suite
(`r/labtracker/tests/testthat`, which also needs the testthat and withr R
packages). CI runners without R skip those tests; `R CMD check` on
`r/labtracker` passes with one expected NOTE (`unlockBinding`, used to put the
ggsave wrapper in ggplot2's namespace).
