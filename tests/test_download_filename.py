from __future__ import annotations

from urllib.parse import unquote

import pytest

from lab_tracker.routes.shared import content_disposition_header


def _extended_filename(header: str) -> str | None:
    marker = "filename*=UTF-8''"
    if marker not in header:
        return None
    return unquote(header.split(marker, 1)[1])


@pytest.mark.parametrize(
    "stored",
    [
        "50%25 off.txt",
        "report 50%.txt",
        "a%41b.csv",
        "%E6%95%B0.csv",
    ],
)
def test_percent_in_stored_filename_is_preserved_not_decoded(stored: str) -> None:
    header = content_disposition_header("attachment", stored)

    assert _extended_filename(header) == stored
    fallback = header.split('filename="', 1)[1].split('"', 1)[0]
    assert "%" not in fallback


def test_form_data_escapes_are_undone_before_sanitizing() -> None:
    # Browsers and httpx send '"', CR and LF in a multipart filename as
    # %22, %0D and %0A; Starlette stores them escaped.
    assert content_disposition_header("attachment", "bad%22%0D%0Aname.txt") == (
        'attachment; filename="bad\'__name.txt"'
    )


def test_encoded_separator_is_not_decoded_into_a_path() -> None:
    header = content_disposition_header("attachment", "..%2Fsecret.txt")

    assert _extended_filename(header) == "..%2Fsecret.txt"


def test_plain_ascii_filename_has_no_extended_parameter() -> None:
    assert content_disposition_header("attachment", "figure 1.png") == (
        'attachment; filename="figure 1.png"'
    )


def test_unicode_filename_keeps_extended_parameter() -> None:
    assert content_disposition_header("attachment", "数据.csv") == (
        "attachment; filename=\"download.csv\"; filename*=UTF-8''%E6%95%B0%E6%8D%AE.csv"
    )
