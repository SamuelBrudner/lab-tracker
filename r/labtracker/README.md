# labtracker (R)

Fail-soft Lab Tracker figure capture for R. `labtracker::autotrack()` captures
figures saved with `ggplot2::ggsave()` and the `png()`, `jpeg()`, `tiff()`,
`bmp()` and `pdf()` file devices as **staged evidence notes**, by running the
Python client's `lt capture file` in the background. See
[docs/lab-tracker-r.md](../../docs/lab-tracker-r.md) for setup, what is
captured, limits and kill switches.

## Two ways to load it

1. **No R install step (recommended).** `lt setup autotrack --r --yes` adds a
   managed block to `~/.Rprofile` (or `R_PROFILE_USER`). The block sources the
   copy of `R/labtracker.R` that ships inside the `lab-tracker` Python package
   (`lab_tracker_client/r_labtracker/labtracker.R`), so upgrading `lt` upgrades
   the R side too. `lt setup autotrack --r --dry-run` shows the diff first;
   `--uninstall --yes` removes the block.
2. **As an R package.** `R CMD INSTALL r/labtracker` (or
   `remotes::install_local("r/labtracker")`), then call
   `labtracker::autotrack()` yourself or keep the `.Rprofile` block, which
   prefers an installed package over the shipped copy.

## Layout

- `R/labtracker.R` — the whole implementation, base R plus `grDevices::`
  calls, so it also works when `sys.source()`d into a plain environment. The
  Python package's copy must stay byte-identical;
  `tests/test_r_autotrack.py::test_shipped_r_source_is_the_r_package_source`
  fails otherwise. After editing, run
  `cp r/labtracker/R/labtracker.R src/lab_tracker_client/r_labtracker/labtracker.R`.
- `tests/testthat/` — testthat suite against a stand-in `lt` shell script
  (POSIX only). Run it with
  `Rscript -e 'testthat::test_dir("r/labtracker/tests/testthat")'`; the Python
  suite runs it too when `Rscript`, testthat and withr are installed.

`R CMD check` reports one NOTE: `unlockBinding()` is how the `ggsave` wrapper
is put into ggplot2's namespace (a traced exit handler would be discarded by
ggsave's own `on.exit()`).
