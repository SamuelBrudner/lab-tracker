# labtracker: fail-soft Lab Tracker figure capture for R.
#
# autotrack() captures figures saved with ggplot2::ggsave() and the png(),
# jpeg(), tiff(), bmp() and pdf() file devices. Each capture shells out, in
# the background, to the Python client's language-neutral entry point:
#
#   lt capture file <path> --kind figure --require-bound
#     --metadata figure_autotracked=true --metadata capture_language=R
#
# so an R figure becomes the same staged note (content hash, host and session
# metadata, offline outbox queue) a Python save does. Nothing here ever raises
# into the user's session: every failure becomes at most one message per
# cause. LAB_TRACKER_AUTOTRACK=0 turns autotrack off.
#
# This one file is the whole implementation. The Python package ships an
# identical copy (lab_tracker_client/r_labtracker/labtracker.R) that
# `lt setup autotrack --r` points ~/.Rprofile at when the R package itself is
# not installed; tests/test_r_autotrack.py keeps the two copies identical. It
# therefore uses only base R plus `grDevices::` calls, and works both as a
# package namespace and when sys.source()d into a plain environment.

.lt_state <- new.env(parent = emptyenv())
.lt_state$installed <- FALSE
.lt_state$options <- list(project_id = NULL, metadata = list(), lt = NULL)
.lt_state$devices <- list()
.lt_state$opening <- list()
.lt_state$suppress <- 0L
.lt_state$suppressed_paths <- character()
.lt_state$pending <- list()
.lt_state$notified <- character()
.lt_state$traced <- character()

# Device function -> the argument that names its output file. svg() is
# deliberately absent: the server refuses image/svg+xml uploads.
.LT_DEVICES <- c(png = "filename", jpeg = "filename", tiff = "filename",
                 bmp = "filename", pdf = "file")
.LT_SUFFIXES <- c("png", "jpg", "jpeg", "tif", "tiff", "bmp", "pdf")
.LT_CALLBACK <- "labtracker-autotrack"
.LT_PENDING_TIMEOUT_SECONDS <- 300
.LT_MAX_PAGES <- 1000L

# Capture figures saved from R into Lab Tracker.
# 
# Wraps ggplot2::ggsave() (re-applied whenever ggplot2 is loaded again) and
# traces the png/jpeg/tiff/bmp/pdf file devices: a device's file is captured
# when dev.off() closes it. Captures run `lt capture file --require-bound` in
# the background, so only saves whose project comes from `project_id`,
# LAB_TRACKER_PROJECT_ID or the checkout's lt_ids.json are captured.
# 
# @param enabled FALSE removes every hook.
# @param project_id Optional project UUID for every capture.
# @param metadata Optional named list of scalar note metadata.
# @param lt Optional path to the `lt` executable, used after
#   LAB_TRACKER_LT and before `lt` on PATH.
# @return Invisibly, whether autotrack is installed afterwards.
autotrack <- function(enabled = TRUE, project_id = NULL, metadata = list(), lt = NULL) {
  if (!isTRUE(enabled)) {
    .lt_uninstall()
    return(invisible(FALSE))
  }
  if (!.lt_env_enabled()) {
    return(invisible(FALSE))
  }
  installed <- tryCatch({
    .lt_state$options <- list(
      project_id = .lt_optional_string(project_id),
      metadata = .lt_metadata_list(metadata),
      lt = .lt_optional_string(lt)
    )
    .lt_trace_devices()
    .lt_register_ggplot2_hook()
    .lt_wrap_ggsave()
    .lt_register_callback()
    .lt_state$installed <- TRUE
    TRUE
  }, error = function(e) {
    .lt_notice(paste0("Lab Tracker autotrack could not install its R hooks: ",
                      conditionMessage(e)))
    .lt_uninstall()
    FALSE
  })
  invisible(installed)
}

# Whether autotrack() hooks are installed in this session.
is_autotracking <- function() {
  isTRUE(.lt_state$installed)
}

# Capture one saved file now (fail-soft).
# 
# @param path The saved file.
# @param kind Evidence kind label (default "figure").
# @param project_id Optional project UUID.
# @param metadata Optional named list of scalar note metadata.
# @param require_bound Capture only into a bound project (the autotrack rule).
# @param wait TRUE waits for `lt` and returns its result; FALSE runs it in
#   the background.
# @return Invisibly, the parsed result (wait = TRUE) or NULL.
capture_file <- function(path, kind = "figure", project_id = NULL, metadata = list(),
                         require_bound = FALSE, wait = FALSE) {
  tryCatch({
    extra <- c(.lt_metadata_list(metadata), list(capture_language = "R"))
    .lt_run_capture(.lt_abs_path(path), kind = kind,
                    project_id = .lt_optional_string(project_id),
                    metadata = extra, require_bound = isTRUE(require_bound),
                    wait = isTRUE(wait))
  }, error = function(e) {
    .lt_notice(paste0("Lab Tracker capture failed: ", conditionMessage(e)))
    invisible(NULL)
  })
}

# Autotrack state for troubleshooting.
autotrack_status <- function() {
  list(
    installed = is_autotracking(),
    kill_switch_set = !.lt_env_enabled(),
    lt = .lt_find_lt(),
    traced_devices = .lt_state$traced,
    ggsave_wrapped = .lt_ggsave_is_wrapped(),
    pending = length(.lt_state$pending)
  )
}

# --- capture -----------------------------------------------------------------

.lt_env_enabled <- function() {
  value <- tolower(trimws(Sys.getenv("LAB_TRACKER_AUTOTRACK", "1")))
  !(value %in% c("0", "false", "no", "off"))
}

.lt_capture_autotracked <- function(path, source) {
  if (!.lt_env_enabled() || !.lt_suffix_matches(path)) {
    return(invisible(NULL))
  }
  if (isTRUE(getOption("knitr.in.progress")) && !identical(source, "ggsave")) {
    # knitr renders every chunk through a file device; the knitted document,
    # not each chunk image, is the artifact.
    return(invisible(NULL))
  }
  options <- .lt_state$options
  metadata <- c(options$metadata, list(figure_autotracked = TRUE,
                                       capture_language = "R",
                                       figure_r_device = source))
  .lt_run_capture(path, kind = "figure", project_id = options$project_id,
                  metadata = metadata, require_bound = TRUE,
                  wait = isTRUE(getOption("labtracker.autotrack.wait", FALSE)))
}

.lt_run_capture <- function(path, kind, project_id, metadata, require_bound, wait) {
  lt <- .lt_find_lt()
  if (!nzchar(lt)) {
    .lt_notice(paste(
      "Lab Tracker autotrack could not find the `lt` command, so R figures are not",
      "captured. Set LAB_TRACKER_LT to its path, put it on PATH, or turn autotrack",
      "off with LAB_TRACKER_AUTOTRACK=0 (`lt setup autotrack --r --uninstall --yes`",
      "removes the ~/.Rprofile block)."
    ))
    return(invisible(NULL))
  }
  output <- tempfile("labtracker-capture-", fileext = ".json")
  args <- c("capture", "file", path, "--kind", kind)
  if (require_bound) args <- c(args, "--require-bound")
  if (!is.null(project_id)) args <- c(args, "--project", project_id)
  for (name in names(metadata)) {
    args <- c(args, "--metadata", paste0(name, "=", .lt_metadata_text(metadata[[name]])))
  }
  args <- c(args, "--output", output)
  launched <- tryCatch({
    # system2() quotes the command itself; the arguments are ours to quote.
    system2(lt, shQuote(args), stdout = FALSE, stderr = FALSE, wait = wait)
    TRUE
  }, error = function(e) {
    .lt_notice(paste0("Lab Tracker could not start `", lt, "`: ", conditionMessage(e)))
    FALSE
  }, warning = function(w) {
    .lt_notice(paste0("Lab Tracker could not start `", lt, "`: ", conditionMessage(w)))
    FALSE
  })
  if (!launched) {
    return(invisible(NULL))
  }
  .lt_state$pending[[output]] <- list(output = output, path = path, started = Sys.time())
  .lt_register_callback()
  if (wait) {
    return(invisible(.lt_poll()[[output]]))
  }
  invisible(NULL)
}

.lt_find_lt <- function() {
  override <- Sys.getenv("LAB_TRACKER_LT", "")
  if (nzchar(override)) {
    return(.lt_executable(override))
  }
  recorded <- .lt_state$options$lt
  if (!is.null(recorded) && file.exists(recorded)) {
    return(recorded)
  }
  .lt_executable("lt")
}

.lt_executable <- function(command) {
  if (file.exists(command) && !dir.exists(command)) {
    return(command)
  }
  found <- unname(Sys.which(command))
  if (length(found) == 1L && !is.na(found) && nzchar(found)) found else ""
}

.lt_metadata_list <- function(metadata) {
  if (is.null(metadata) || length(metadata) == 0L) {
    return(list())
  }
  metadata <- as.list(metadata)
  keys <- names(metadata)
  if (is.null(keys) || any(!nzchar(keys))) {
    stop("metadata must be a named list of scalar values")
  }
  for (key in keys) {
    value <- metadata[[key]]
    if (length(value) != 1L || !(is.character(value) || is.numeric(value) || is.logical(value)) ||
        is.na(value)) {
      stop(sprintf("metadata value for '%s' must be one string, number or TRUE/FALSE", key))
    }
  }
  metadata
}

.lt_metadata_text <- function(value) {
  if (is.logical(value)) {
    return(if (isTRUE(value)) "true" else "false")
  }
  if (is.numeric(value)) {
    return(format(value, digits = 15, scientific = FALSE, trim = TRUE))
  }
  as.character(value)
}

.lt_optional_string <- function(value) {
  if (is.null(value) || length(value) != 1L || is.na(value)) {
    return(NULL)
  }
  text <- trimws(as.character(value))
  if (nzchar(text)) text else NULL
}

# --- results and messages ------------------------------------------------------

.lt_notice <- function(text) {
  if (!nzchar(text) || text %in% .lt_state$notified) {
    return(invisible(FALSE))
  }
  .lt_state$notified <- c(.lt_state$notified, text)
  tryCatch(message(text), error = function(e) NULL)
  invisible(TRUE)
}

# Reads finished `lt capture file --output` results and shows each notice
# once; returns the parsed results keyed by output path.
.lt_poll <- function() {
  results <- list()
  for (key in names(.lt_state$pending)) {
    entry <- .lt_state$pending[[key]]
    if (file.exists(entry$output)) {
      text <- tryCatch(paste(readLines(entry$output, warn = FALSE, encoding = "UTF-8"),
                             collapse = "\n"),
                       error = function(e) "")
      unlink(entry$output)
      .lt_state$pending[[key]] <- NULL
      result <- .lt_parse_result(text)
      for (notice in result$notices) .lt_notice(notice)
      results[[key]] <- result
    } else if (difftime(Sys.time(), entry$started, units = "secs") >
               .LT_PENDING_TIMEOUT_SECONDS) {
      .lt_state$pending[[key]] <- NULL
      .lt_notice(paste(
        "Lab Tracker: `lt capture file` did not report back within 5 minutes;",
        "run it by hand on the figure to see why."
      ))
    }
  }
  results
}

.lt_task_callback <- function(expr, value, ok, visible) {
  tryCatch(if (length(.lt_state$pending)) .lt_poll(), error = function(e) NULL)
  TRUE
}

.lt_register_callback <- function() {
  if (!(.LT_CALLBACK %in% getTaskCallbackNames())) {
    addTaskCallback(.lt_task_callback, name = .LT_CALLBACK)
  }
  invisible(NULL)
}

.lt_parse_result <- function(text) {
  list(
    action = .lt_json_top_string(text, "action"),
    reason = .lt_json_top_string(text, "reason"),
    notices = .lt_json_top_strings(text, "notices"),
    raw = text
  )
}

# The result file is written with indent=2, so a top-level key is the one on
# a line indented by exactly two spaces.
.LT_JSON_STRING <- "\"((?:[^\"\\\\]|\\\\.)*)\""

.lt_json_top_string <- function(text, key) {
  pattern <- paste0("\n  \"", key, "\": ", .LT_JSON_STRING)
  match <- regmatches(text, regexec(pattern, text, perl = TRUE))[[1]]
  if (length(match) < 2L) NA_character_ else .lt_json_unescape(match[[2]])
}

.lt_json_top_strings <- function(text, key) {
  start <- regexpr(paste0("\n  \"", key, "\": \\["), text, perl = TRUE)
  if (start < 0L) {
    return(character())
  }
  rest <- substring(text, start + attr(start, "match.length"))
  if (grepl("^\\s*\\]", rest)) {
    return(character())
  }
  # Items are one per line and a string never holds a raw newline, so the
  # array ends at the first line that is exactly the two-space "]".
  end <- regexpr("\n  ]", rest, fixed = TRUE)
  body <- if (end < 0L) rest else substr(rest, 1L, end - 1L)
  tokens <- regmatches(body, gregexpr(.LT_JSON_STRING, body, perl = TRUE))[[1]]
  values <- vapply(tokens, function(token) {
    .lt_json_unescape(substr(token, 2L, nchar(token) - 1L))
  }, character(1), USE.NAMES = FALSE)
  values[nzchar(values)]
}

.lt_json_unescape <- function(text) {
  if (!grepl("\\", text, fixed = TRUE)) {
    return(text)
  }
  chars <- strsplit(text, "", fixed = TRUE)[[1]]
  out <- character(length(chars))
  count <- 0L
  index <- 1L
  total <- length(chars)
  while (index <= total) {
    char <- chars[[index]]
    if (char == "\\" && index < total) {
      escape <- chars[[index + 1L]]
      if (escape == "u" && index + 5L <= total) {
        code <- strtoi(paste(chars[(index + 2L):(index + 5L)], collapse = ""), 16L)
        index <- index + 6L
        high <- !is.na(code) && code >= 0xD800 && code <= 0xDBFF
        if (high && index + 5L <= total && chars[[index]] == "\\" && chars[[index + 1L]] == "u") {
          low <- strtoi(paste(chars[(index + 2L):(index + 5L)], collapse = ""), 16L)
          if (!is.na(low) && low >= 0xDC00 && low <= 0xDFFF) {
            code <- 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
            index <- index + 6L
          }
        }
        decoded <- if (is.na(code)) NA_character_ else intToUtf8(code)
        count <- count + 1L
        out[[count]] <- if (is.na(decoded)) "?" else decoded
        next
      }
      count <- count + 1L
      out[[count]] <- switch(escape, n = "\n", t = "\t", r = "\r", b = "\b", f = "\f", escape)
      index <- index + 2L
      next
    }
    count <- count + 1L
    out[[count]] <- char
    index <- index + 1L
  }
  paste(out[seq_len(count)], collapse = "")
}

# --- paths ---------------------------------------------------------------------

.lt_abs_path <- function(path) {
  path <- path.expand(as.character(path))
  absolute <- grepl("^(/|[A-Za-z]:[/\\\\]|\\\\\\\\)", path)
  if (absolute) path else file.path(getwd(), path)
}

.lt_suffix_matches <- function(path) {
  suffix <- tolower(sub("^.*\\.", "", basename(path)))
  grepl(".", basename(path), fixed = TRUE) && suffix %in% .LT_SUFFIXES
}

.lt_device_path <- function(file) {
  if (!is.character(file) || length(file) != 1L || is.na(file) || !nzchar(file)) {
    return(NULL)
  }
  if (startsWith(file, "|")) {
    return(NULL)
  }
  .lt_abs_path(file)
}

# The files a closed device wrote: a page pattern ("plot%03d.png") expands to
# each page saved since the device opened; otherwise the file itself.
.lt_device_files <- function(record) {
  since <- record$opened - 2
  fresh <- function(candidate) {
    file.exists(candidate) && !dir.exists(candidate) &&
      isTRUE(file.mtime(candidate) >= since)
  }
  path <- record$path
  if (grepl("%", path, fixed = TRUE)) {
    pages <- character()
    for (page in seq_len(.LT_MAX_PAGES)) {
      candidate <- tryCatch(sprintf(path, page), error = function(e) NA_character_)
      if (is.na(candidate) || identical(candidate, path) || !file.exists(candidate)) break
      if (fresh(candidate)) pages <- c(pages, candidate)
    }
    if (length(pages)) {
      return(pages)
    }
  }
  if (fresh(path)) path else character()
}

# --- graphics devices ----------------------------------------------------------

.lt_open_devices <- function() {
  devices <- grDevices::dev.list()
  if (is.null(devices)) integer() else as.integer(devices)
}

.lt_device_entry <- function() {
  tryCatch({
    .lt_state$opening <- c(list(.lt_open_devices()), .lt_state$opening)
  }, error = function(e) NULL)
  invisible(NULL)
}

.lt_device_exit <- function(device, file, default_file) {
  tryCatch({
    before <- if (length(.lt_state$opening)) .lt_state$opening[[1L]] else integer()
    .lt_state$opening <- .lt_state$opening[-1L]
    current <- as.integer(grDevices::dev.cur())
    if (current > 1L && !(current %in% before)) {
      key <- as.character(current)
      # A device opened without a file name (the "Rplot%03d.png" default)
      # was not an explicit save, just as matplotlib autotrack captures only
      # savefig() to a path.
      path <- if (isTRUE(default_file)) NULL else .lt_device_path(file)
      if (is.null(path)) {
        .lt_state$devices[[key]] <- NULL
      } else {
        .lt_state$devices[[key]] <- list(path = path, device = device, opened = Sys.time(),
                                          suppressed = .lt_state$suppress > 0L)
      }
    }
  }, error = function(e) NULL)
  invisible(NULL)
}

.lt_device_closed <- function(which) {
  tryCatch({
    key <- as.character(as.integer(which))
    record <- .lt_state$devices[[key]]
    if (!is.null(record)) {
      .lt_state$devices[[key]] <- NULL
      files <- .lt_device_files(record)
      if (isTRUE(record$suppressed) || .lt_state$suppress > 0L) {
        # Saved inside ggsave(): its wrapper captures the file once.
        .lt_state$suppressed_paths <- c(.lt_state$suppressed_paths, files)
      } else {
        for (path in files) .lt_capture_autotracked(path, record$device)
      }
    }
  }, error = function(e) NULL)
  invisible(NULL)
}

.lt_trace_where <- function() {
  # Tracing through the attached package environment updates both it and the
  # namespace; before grDevices is attached only the namespace exists, and
  # attaching copies the traced function out of it.
  if ("package:grDevices" %in% search()) {
    as.environment("package:grDevices")
  } else {
    asNamespace("grDevices")
  }
}

.lt_trace_devices <- function() {
  where <- .lt_trace_where()
  traced <- character()
  for (device in names(.LT_DEVICES)) {
    argument <- as.name(.LT_DEVICES[[device]])
    exit <- bquote(.(.lt_device_exit)(.(device), .(argument), missing(.(argument))))
    suppressMessages(trace(device, tracer = bquote(.(.lt_device_entry)()), exit = exit,
                           print = FALSE, where = where))
    traced <- c(traced, device)
  }
  suppressMessages(trace("dev.off", exit = bquote(.(.lt_device_closed)(which)),
                         print = FALSE, where = where))
  .lt_state$traced <- c(traced, "dev.off")
  invisible(traced)
}

.lt_untrace_devices <- function() {
  if (!length(.lt_state$traced) || !("grDevices" %in% loadedNamespaces())) {
    .lt_state$traced <- character()
    return(invisible(NULL))
  }
  where <- .lt_trace_where()
  for (name in .lt_state$traced) {
    tryCatch(suppressMessages(untrace(name, where = where)), error = function(e) NULL)
  }
  .lt_state$traced <- character()
  invisible(NULL)
}

# --- ggplot2::ggsave -----------------------------------------------------------

# ggsave() closes its device in an on.exit() that replaces any trace(exit=)
# handler, so it is wrapped rather than traced: the wrapper keeps ggsave's
# formals, calls the original exactly as the user did, and captures the file
# it reports once it has been written and closed.
.lt_make_ggsave <- function(original) {
  wrapper <- function() {
    caller <- parent.frame()
    call <- sys.call()
    call[[1L]] <- original
    .lt_state$suppress <- .lt_state$suppress + 1L
    if (.lt_state$suppress == 1L) .lt_state$suppressed_paths <- character()
    result <- tryCatch(eval(call, caller),
                       finally = .lt_state$suppress <- max(0L, .lt_state$suppress - 1L))
    if (.lt_state$suppress == 0L) .lt_after_ggsave(result)
    invisible(result)
  }
  formals(wrapper) <- formals(original)
  attr(wrapper, "labtracker_original") <- original
  wrapper
}

.lt_after_ggsave <- function(result) {
  tryCatch({
    paths <- if (is.character(result) && length(result) == 1L && !is.na(result)) {
      .lt_abs_path(result)
    } else {
      unique(.lt_state$suppressed_paths)
    }
    .lt_state$suppressed_paths <- character()
    for (path in paths) {
      if (file.exists(path)) .lt_capture_autotracked(path, "ggsave")
    }
  }, error = function(e) NULL)
  invisible(NULL)
}

.lt_replace_binding <- function(env, name, value) {
  locked <- bindingIsLocked(name, env)
  if (locked) unlockBinding(name, env)
  assign(name, value, envir = env)
  if (locked) lockBinding(name, env)
  invisible(NULL)
}

.lt_ggsave_is_wrapped <- function() {
  if (!("ggplot2" %in% loadedNamespaces())) {
    return(FALSE)
  }
  current <- get0("ggsave", envir = asNamespace("ggplot2"), inherits = FALSE)
  !is.null(attr(current, "labtracker_original"))
}

.lt_ggsave_envs <- function() {
  envs <- list(asNamespace("ggplot2"))
  if ("package:ggplot2" %in% search()) {
    envs <- c(envs, list(as.environment("package:ggplot2")))
  }
  envs
}

.lt_wrap_ggsave <- function() {
  if (!("ggplot2" %in% loadedNamespaces())) {
    return(invisible(FALSE))
  }
  current <- get0("ggsave", envir = asNamespace("ggplot2"), inherits = FALSE)
  if (!is.function(current)) {
    return(invisible(FALSE))
  }
  # Unwrap first, so sourcing this file again never stacks two wrappers.
  original <- attr(current, "labtracker_original")
  if (is.null(original)) original <- current
  wrapper <- .lt_make_ggsave(original)
  for (env in .lt_ggsave_envs()) {
    if (exists("ggsave", envir = env, inherits = FALSE)) {
      .lt_replace_binding(env, "ggsave", wrapper)
    }
  }
  invisible(TRUE)
}

.lt_unwrap_ggsave <- function() {
  if (!("ggplot2" %in% loadedNamespaces())) {
    return(invisible(FALSE))
  }
  for (env in .lt_ggsave_envs()) {
    current <- get0("ggsave", envir = env, inherits = FALSE)
    original <- attr(current, "labtracker_original")
    if (!is.null(original)) .lt_replace_binding(env, "ggsave", original)
  }
  invisible(TRUE)
}

.lt_ggplot2_loaded <- function(...) {
  tryCatch(if (is_autotracking()) .lt_wrap_ggsave(), error = function(e) NULL)
  invisible(NULL)
}
attr(.lt_ggplot2_loaded, "labtracker_hook") <- TRUE

.lt_other_hooks <- function(event) {
  Filter(function(hook) is.null(attr(hook, "labtracker_hook")), getHook(event))
}

.lt_register_ggplot2_hook <- function() {
  # Re-applies the wrapper every time ggplot2's namespace is loaded again
  # (for example after unloadNamespace() or devtools::load_all()).
  event <- packageEvent("ggplot2", "onLoad")
  setHook(event, c(.lt_other_hooks(event), list(.lt_ggplot2_loaded)), action = "replace")
  invisible(NULL)
}

.lt_remove_ggplot2_hook <- function() {
  event <- packageEvent("ggplot2", "onLoad")
  others <- .lt_other_hooks(event)
  setHook(event, if (length(others)) others else NULL, action = "replace")
  invisible(NULL)
}

.lt_uninstall <- function() {
  tryCatch({
    .lt_untrace_devices()
    .lt_unwrap_ggsave()
    .lt_remove_ggplot2_hook()
    if (.LT_CALLBACK %in% getTaskCallbackNames()) removeTaskCallback(.LT_CALLBACK)
  }, error = function(e) NULL)
  .lt_state$installed <- FALSE
  .lt_state$devices <- list()
  .lt_state$opening <- list()
  invisible(NULL)
}
