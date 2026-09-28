skip_on_os("windows")  # the stand-in `lt` is a POSIX shell script
skip_if_not_installed("ggplot2")

a_plot <- function() {
  ggplot2::ggplot(data.frame(x = 1:3, y = c(2, 1, 3)), ggplot2::aes(x, y)) +
    ggplot2::geom_point()
}

test_that("ggsave() is captured exactly once, not again by its device", {
  fx <- local_autotrack()
  expect_true(lt$autotrack_status()$ggsave_wrapped)
  path <- file.path(fx$dir, "gg.png")

  ggplot2::ggsave(path, a_plot(), width = 2, height = 2, dpi = 50)

  calls <- lt_calls(fx$log)
  expect_length(calls, 1L)
  expect_equal(calls[[1]][[3]], path)
  expect_true("figure_r_device=ggsave" %in% metadata_args(calls[[1]]))
})

test_that("ggsave() keeps its formals, path argument and return value", {
  fx <- local_autotrack()
  wrapped <- get("ggsave", envir = asNamespace("ggplot2"))
  expect_equal(names(formals(wrapped)), names(formals(attr(wrapped, "labtracker_original"))))

  withr::local_dir(fx$dir)
  dir.create("figs")
  returned <- ggplot2::ggsave("sub.pdf", a_plot(), path = "figs", width = 2, height = 2)
  expect_equal(returned, file.path("figs", "sub.pdf"))
  expect_equal(lt_calls(fx$log)[[1]][[3]], file.path(getwd(), "figs", "sub.pdf"))
})

test_that("an svg ggsave is not captured", {
  skip_if_not_installed("svglite")
  fx <- local_autotrack()
  ggplot2::ggsave(file.path(fx$dir, "gg.svg"), a_plot(), width = 2, height = 2)
  expect_length(lt_calls(fx$log), 0L)
})

test_that("a failing ggsave() raises the user's error and captures nothing", {
  fx <- local_autotrack()
  expect_error(ggplot2::ggsave(file.path(fx$dir, "gg.unknownext"), a_plot()))
  expect_length(lt_calls(fx$log), 0L)
  expect_equal(lt$.lt_state$suppress, 0L)
})

test_that("the ggsave wrapper survives ggplot2 being unloaded and loaded again", {
  fx <- local_autotrack()
  attached <- "package:ggplot2" %in% search()
  unloaded <- tryCatch({
    unloadNamespace("ggplot2")
    TRUE
  }, error = function(e) FALSE)
  skip_if_not(unloaded, "ggplot2 is in use by another loaded package")
  loadNamespace("ggplot2")
  if (attached) suppressPackageStartupMessages(library(ggplot2))

  expect_true(lt$autotrack_status()$ggsave_wrapped)
  ggplot2::ggsave(file.path(fx$dir, "reloaded.png"), a_plot(), width = 2, height = 2, dpi = 50)
  expect_length(lt_calls(fx$log), 1L)
})

test_that("autotrack(FALSE) restores the original ggsave and drops the load hook", {
  fx <- local_autotrack()
  lt$autotrack(FALSE)
  current <- get("ggsave", envir = asNamespace("ggplot2"))
  expect_null(attr(current, "labtracker_original"))
  hooks <- getHook(packageEvent("ggplot2", "onLoad"))
  expect_false(any(vapply(hooks, function(h) !is.null(attr(h, "labtracker_hook")), logical(1))))
  ggplot2::ggsave(file.path(fx$dir, "off.png"), a_plot(), width = 2, height = 2, dpi = 50)
  expect_length(lt_calls(fx$log), 0L)
})
