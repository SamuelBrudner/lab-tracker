from __future__ import annotations

import pytest

from lab_tracker.local_path_syntax import parse_windows_absolute_local_path


@pytest.mark.parametrize(
    ("raw", "rendered"),
    (
        (r"c:/Allowed/store", r"C:\Allowed\store"),
        ("c:\\Allowed\\store\\", r"C:\Allowed\store"),
        (r"c:\Allowed\\store", r"C:\Allowed\store"),
        ("c:\\", "C:\\"),
    ),
)
def test_windows_parser_normalizes_only_safe_spelling_aliases(
    raw: str,
    rendered: str,
) -> None:
    parsed = parse_windows_absolute_local_path(raw, allow_navigation=False)

    assert parsed is not None
    assert parsed.rendered == rendered


@pytest.mark.parametrize(
    "raw",
    (
        r"\\server\share",
        r"\\?\C:\store",
        r"\\.\PhysicalDrive0",
        r"C:relative",
        r"C:\Allowed\..",
        r"C:\Allowed\.",
        r"C:\Allowed\CON",
        r"C:\Allowed\trailing.",
        r"C:\Allowed\artifact:stream",
    ),
)
def test_windows_root_parser_rejects_namespace_and_component_ambiguity(
    raw: str,
) -> None:
    assert (
        parse_windows_absolute_local_path(raw, allow_navigation=False)
        is None
    )


def test_windows_candidate_parser_can_preserve_navigation_tokens() -> None:
    parsed = parse_windows_absolute_local_path(
        r"c:\Allowed\link\..\store",
        allow_navigation=True,
    )

    assert parsed is not None
    assert parsed.rendered == r"C:\Allowed\link\..\store"
    assert parsed.components == ("Allowed", "link", "..", "store")
