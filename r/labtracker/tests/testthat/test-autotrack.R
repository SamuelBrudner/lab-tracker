skip_on_os("windows")  # the stand-in `lt` is a POSIX shell script

save_plot <- function(device, path, ...) {
  device(path, ...)
  graphics::plot(1:3)
  grDevices::dev.off()
  invisible(path)
}

test_that("a png closed by dev.off() is captured once, bound and labelled", {
  fx <- local_autotrack()
  expect_true(fx$installed)
  expect_true(lt$is_autotracking())
  path <- file.path(fx$dir, "trace.png")

  save_plot(grDevices::png, path)

  calls <- lt_calls(fx$log)
  expect_length(calls, 1L)
  args <- calls[[1]]
  expect_equal(args[1:3], c("capture", "file", path))
  expect_equal(arg_after(args, "--kind"), "figure")
  expect_true("--require-bound" %in% args)
  expect_setequal(metadata_args(args), c("figure_autotracked=true", "capture_language=R",
                                         "figure_r_device=png"))
  expect_false(is.na(arg_after(args, "--output")))
})

test_that("unqualified device calls are captured too", {
  fx <- local_autotrack()
  path <- file.path(fx$dir, "plain.png")
  png(path)
  plot(1)
  dev.off()
  expect_length(lt_calls(fx$log), 1L)
})

test_that("jpeg, tiff, bmp and pdf files are captured; svg is not", {
  fx <- local_autotrack()
  save_plot(grDevices::jpeg, file.path(fx$dir, "a.jpg"))
  save_plot(grDevices::tiff, file.path(fx$dir, "b.tiff"))
  save_plot(grDevices::bmp, file.path(fx$dir, "c.bmp"))
  save_plot(grDevices::pdf, file.path(fx$dir, "d.pdf"))
  if (capabilities("cairo")) save_plot(grDevices::svg, file.path(fx$dir, "e.svg"))

  captured <- vapply(lt_calls(fx$log), function(args) basename(args[[3]]), character(1))
  expect_equal(captured, c("a.jpg", "b.tiff", "c.bmp", "d.pdf"))
  devices <- vapply(lt_calls(fx$log), function(args) {
    grep("^figure_r_device=", metadata_args(args), value = TRUE)
  }, character(1))
  expect_equal(devices, paste0("figure_r_device=", c("jpeg", "tiff", "bmp", "pdf")))
})

test_that("a device opened without a file name is not an explicit save", {
  fx <- local_autotrack()
  withr::local_dir(fx$dir)
  grDevices::png()
  graphics::plot(1)
  grDevices::dev.off()
  expect_length(lt_calls(fx$log), 0L)
})

test_that("each page of a page-numbered device is captured", {
  fx <- local_autotrack()
  grDevices::png(file.path(fx$dir, "page%02d.png"))
  graphics::plot(1)
  graphics::plot(2)
  grDevices::dev.off()
  captured <- vapply(lt_calls(fx$log), function(args) basename(args[[3]]), character(1))
  expect_equal(captured, c("page01.png", "page02.png"))
})

test_that("a relative file name is captured by its absolute path", {
  fx <- local_autotrack()
  withr::local_dir(fx$dir)
  save_plot(grDevices::png, "relative.png")
  expect_equal(lt_calls(fx$log)[[1]][[3]], file.path(getwd(), "relative.png"))
})

test_that("graphics.off() captures every open file device", {
  fx <- local_autotrack()
  grDevices::png(file.path(fx$dir, "one.png"))
  graphics::plot(1)
  grDevices::pdf(file.path(fx$dir, "two.pdf"))
  graphics::plot(2)
  grDevices::graphics.off()
  captured <- sort(vapply(lt_calls(fx$log), function(args) basename(args[[3]]), character(1)))
  expect_equal(captured, c("one.png", "two.pdf"))
})

test_that("project and metadata options reach every capture", {
  fx <- local_autotrack(project_id = "project-7", metadata = list(rig = "2", trial = 3L))
  save_plot(grDevices::png, file.path(fx$dir, "opt.png"))
  args <- lt_calls(fx$log)[[1]]
  expect_equal(arg_after(args, "--project"), "project-7")
  expect_true(all(c("rig=2", "trial=3") %in% metadata_args(args)))
})

test_that("the kill switch stops installing and stops capturing", {
  fx <- local_autotrack()
  withr::local_envvar(LAB_TRACKER_AUTOTRACK = "off")
  save_plot(grDevices::png, file.path(fx$dir, "off.png"))
  expect_length(lt_calls(fx$log), 0L)

  lt$autotrack(FALSE)
  expect_false(lt$autotrack())
  expect_false(lt$is_autotracking())
})

test_that("autotrack(FALSE) restores every device", {
  fx <- local_autotrack()
  expect_true(inherits(get("png", envir = as.environment("package:grDevices")),
                       "functionWithTrace"))
  lt$autotrack(FALSE)
  for (name in c("png", "jpeg", "tiff", "bmp", "pdf", "dev.off")) {
    expect_false(inherits(get(name, envir = asNamespace("grDevices")), "functionWithTrace"))
    expect_false(inherits(get(name, envir = as.environment("package:grDevices")),
                          "functionWithTrace"))
  }
  save_plot(grDevices::png, file.path(fx$dir, "after.png"))
  expect_length(lt_calls(fx$log), 0L)
  expect_false("labtracker-autotrack" %in% getTaskCallbackNames())
})

test_that("installing twice keeps one set of hooks", {
  fx <- local_autotrack()
  lt$autotrack()
  save_plot(grDevices::png, file.path(fx$dir, "twice.png"))
  expect_length(lt_calls(fx$log), 1L)
  expect_equal(sum(getTaskCallbackNames() == "labtracker-autotrack"), 1L)
})

test_that("a missing lt is reported once and never errors", {
  fx <- local_autotrack()
  withr::local_envvar(LAB_TRACKER_LT = file.path(fx$dir, "no-such-lt"), PATH = fx$dir)
  unlink(file.path(fx$dir, "lt"))
  expect_message(save_plot(grDevices::png, file.path(fx$dir, "x.png")), "could not find the `lt`")
  expect_no_message(save_plot(grDevices::png, file.path(fx$dir, "y.png")))
})

test_that("each notice from lt is shown once per session", {
  fx <- local_autotrack()
  withr::local_envvar(FAKE_LT_ACTION = "skipped",
                      FAKE_LT_NOTICE = "Lab Tracker autotrack is not capturing saves in /x")
  expect_message(save_plot(grDevices::png, file.path(fx$dir, "a.png")),
                 "not capturing saves in /x")
  expect_no_message(save_plot(grDevices::png, file.path(fx$dir, "b.png")))
  withr::local_envvar(FAKE_LT_NOTICE = "Lab Tracker is unreachable; queued")
  expect_message(save_plot(grDevices::png, file.path(fx$dir, "c.png")), "unreachable")
})

test_that("a device call that fails records nothing and the error is the user's own", {
  fx <- local_autotrack()
  expect_error(grDevices::png(file.path(fx$dir, "bad.png"), width = -1))
  expect_length(lt_calls(fx$log), 0L)
  expect_length(lt$.lt_state$opening, 0L)
})

test_that("background captures do not block the session", {
  fx <- local_autotrack()
  withr::local_options(list(labtracker.autotrack.wait = FALSE))
  withr::local_envvar(FAKE_LT_SLEEP = "2", FAKE_LT_NOTICE = "background notice")
  started <- Sys.time()
  save_plot(grDevices::png, file.path(fx$dir, "bg.png"))
  expect_lt(as.numeric(difftime(Sys.time(), started, units = "secs")), 1.5)
  expect_length(lt$.lt_state$pending, 1L)

  deadline <- Sys.time() + 20
  shown <- FALSE
  while (!shown && Sys.time() < deadline) {
    shown <- length(testthat::capture_messages(lt$.lt_poll())) > 0L
    if (!shown) Sys.sleep(0.1)
  }
  expect_true(shown)
  expect_length(lt$.lt_state$pending, 0L)
})

test_that("capture_file() runs an explicit capture and returns its result", {
  fx <- local_autotrack()
  path <- file.path(fx$dir, "explicit.png")
  save_plot(grDevices::png, path)
  result <- lt$capture_file(path, metadata = list(stage = "final"), wait = TRUE)
  expect_equal(result$action, "imported")
  args <- lt_calls(fx$log)[[2]]
  expect_false("--require-bound" %in% args)
  expect_true(all(c("stage=final", "capture_language=R") %in% metadata_args(args)))
})
