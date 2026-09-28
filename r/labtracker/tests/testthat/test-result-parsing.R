test_that("the capture result's action, reason and notices are read", {
  text <- paste(
    "{",
    '  "action": "skipped",',
    '  "metadata": {',
    '    "action": "nested value is ignored",',
    '    "notices": "nested too"',
    "  },",
    '  "notices": [',
    '    "Lab Tracker autotrack is not capturing saves in /tmp/a \\"b\\": why.",',
    '    "caf\\u00e9 \\ud83d\\ude00 tab\\tend"',
    "  ],",
    '  "reason": "project_unbound"',
    "}",
    sep = "\n"
  )
  result <- lt$.lt_parse_result(text)
  expect_equal(result$action, "skipped")
  expect_equal(result$reason, "project_unbound")
  expect_equal(result$notices, c(
    "Lab Tracker autotrack is not capturing saves in /tmp/a \"b\": why.",
    paste0("café ", intToUtf8(0x1F600), " tab\tend")
  ))
})

test_that("an empty or absent notices list reads as no notices", {
  expect_equal(lt$.lt_parse_result('{\n  "action": "imported",\n  "notices": []\n}')$notices,
               character())
  expect_equal(lt$.lt_parse_result('{\n  "action": "imported"\n}')$notices, character())
  expect_true(is.na(lt$.lt_parse_result("not json")$action))
})

test_that("metadata values are passed as lt parses them back", {
  expect_equal(lt$.lt_metadata_text(TRUE), "true")
  expect_equal(lt$.lt_metadata_text(FALSE), "false")
  expect_equal(lt$.lt_metadata_text(3L), "3")
  expect_equal(lt$.lt_metadata_text(1.5), "1.5")
  expect_equal(lt$.lt_metadata_text("R"), "R")
  expect_error(lt$.lt_metadata_list(list(1, 2)), "named list")
  expect_error(lt$.lt_metadata_list(list(a = c(1, 2))), "one string")
})

test_that("only figure suffixes the server accepts are captured", {
  expect_true(lt$.lt_suffix_matches("/a/b.PNG"))
  expect_true(lt$.lt_suffix_matches("/a/b.tif"))
  expect_false(lt$.lt_suffix_matches("/a/b.svg"))
  expect_false(lt$.lt_suffix_matches("/a/png"))
})
