# The implementation under test: the installed package's namespace under
# R CMD check, otherwise R/labtracker.R sourced into a plain environment
# (exactly how the ~/.Rprofile block loads the copy shipped with `lt`).
lt <- if ("labtracker" %in% loadedNamespaces()) {
  asNamespace("labtracker")
} else {
  local({
    env <- new.env(parent = baseenv())
    sys.source(test_path("..", "..", "R", "labtracker.R"), envir = env)
    env
  })
}

# A stand-in `lt`: logs its arguments (one per line, then "--end--") to
# $FAKE_LT_LOG and writes an indent=2 result to its --output file, as the
# real `lt capture file --output` does. FAKE_LT_ACTION, FAKE_LT_NOTICE and
# FAKE_LT_SLEEP shape the result.
fake_lt <- function(dir) {
  path <- file.path(dir, "lt")
  writeLines(c(
    "#!/bin/sh",
    "if [ -n \"$FAKE_LT_SLEEP\" ]; then sleep \"$FAKE_LT_SLEEP\"; fi",
    "{ for a in \"$@\"; do printf '%s\\n' \"$a\"; done; printf '%s\\n' '--end--'; } >> \"$FAKE_LT_LOG\"",
    "out=''; prev=''",
    "for a in \"$@\"; do if [ \"$prev\" = '--output' ]; then out=\"$a\"; fi; prev=\"$a\"; done",
    "if [ -n \"$out\" ]; then",
    "  printf '{\\n  \"action\": \"%s\",\\n  \"notices\": [\\n    \"%s\"\\n  ],\\n  \"path\": \"x\"\\n}\\n' \\",
    "    \"${FAKE_LT_ACTION:-imported}\" \"${FAKE_LT_NOTICE:-}\" > \"$out.tmp\" && mv \"$out.tmp\" \"$out\"",
    "fi"
  ), path)
  Sys.chmod(path, "0755")
  path
}

# Every capture the fake `lt` was asked for, as character vectors of args.
lt_calls <- function(log) {
  if (!file.exists(log)) {
    return(list())
  }
  lines <- readLines(log, warn = FALSE)
  calls <- list()
  current <- character()
  for (line in lines) {
    if (identical(line, "--end--")) {
      calls[[length(calls) + 1L]] <- current
      current <- character()
    } else {
      current <- c(current, line)
    }
  }
  calls
}

arg_after <- function(args, flag) {
  index <- match(flag, args)
  if (is.na(index)) NA_character_ else args[[index + 1L]]
}

metadata_args <- function(args) {
  args[which(args == "--metadata") + 1L]
}

# Installs autotrack against a fake `lt` in a scratch directory, removed
# (with every hook) when the calling test ends.
local_autotrack <- function(..., env = parent.frame()) {
  dir <- withr::local_tempdir(.local_envir = env)
  log <- file.path(dir, "lt.log")
  withr::local_envvar(
    c(LAB_TRACKER_LT = fake_lt(dir), FAKE_LT_LOG = log, LAB_TRACKER_AUTOTRACK = NA,
      FAKE_LT_ACTION = NA, FAKE_LT_NOTICE = NA, FAKE_LT_SLEEP = NA),
    .local_envir = env
  )
  withr::local_options(list(labtracker.autotrack.wait = TRUE), .local_envir = env)
  assign("notified", character(), envir = lt$.lt_state)
  withr::defer(lt$autotrack(FALSE), envir = env)
  installed <- lt$autotrack(...)
  list(dir = dir, log = log, installed = installed)
}
