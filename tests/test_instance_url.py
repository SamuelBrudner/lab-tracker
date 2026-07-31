from __future__ import annotations

import pytest

from lab_tracker.instance_url import (
    build_instance_url,
    normalize_instance_base_url,
    resolve_instance_base_url,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://lab.example.test", "https://lab.example.test"),
        ("https://lab.example.test/", "https://lab.example.test"),
        ("https://lab.example.test/app", "https://lab.example.test"),
        ("https://lab.example.test/app/", "https://lab.example.test"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
    ],
)
def test_normalize_instance_base_url(value: str, expected: str) -> None:
    assert normalize_instance_base_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "lab.example.test",
        "ftp://lab.example.test",
        "https://user:secret@lab.example.test",
        "https://lab.example.test/api",
        "https://lab.example.test?mode=demo",
        "https://lab.example.test#graph",
    ],
)
def test_normalize_instance_base_url_rejects_non_origins(value: str) -> None:
    with pytest.raises(ValueError, match="LAB_TRACKER_BASE_URL"):
        normalize_instance_base_url(value)


def test_resolve_instance_base_url_uses_explicit_precedence() -> None:
    assert (
        resolve_instance_base_url(
            (
                ("LAB_TRACKER_BASE_URL", "https://canonical.example.test/app"),
                ("LAB_TRACKER_MCP_BASE_URL", "https://legacy.example.test"),
            )
        )
        == "https://canonical.example.test"
    )


def test_build_instance_url_derives_browser_route() -> None:
    assert build_instance_url("https://lab.example.test/", "/app") == (
        "https://lab.example.test/app"
    )
