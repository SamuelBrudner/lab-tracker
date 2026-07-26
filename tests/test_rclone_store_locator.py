from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.local_store_locator import PortableStorePath
from lab_tracker.rclone_store_locator import (
    RcloneRemoteName,
    RegisteredRcloneRoot,
)


@pytest.mark.parametrize(
    "value",
    (
        "lab",
        "Lab_1-archive.v2+raw@org",
        "Lab Drive 2",
        "研究１２",
        "é",
        "Kelvin",
    ),
)
def test_rclone_remote_name_accepts_documented_exact_grammar(value: str):
    remote = RcloneRemoteName.parse(value)

    assert remote is not None
    assert remote.value == value


def test_rclone_remote_name_checks_nfkc_without_normalizing_the_value():
    value = "Kelvin"

    remote = RcloneRemoteName.parse(value)

    assert remote is not None
    assert remote.value == value
    assert remote.value != "Kelvin"


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        " lab",
        "-lab",
        "lab ",
        "lab\tname",
        "lab/name",
        r"lab\name",
        "lab:name",
        "lab,name",
        "lab=name",
        "lab?name",
        "lab#name",
        "lab$name",
        "C",
        "z",
        "K",
        "Ŀab",
        "\ud800",
    ),
)
def test_rclone_remote_name_rejects_ambiguous_or_nonportable_values(value: object):
    assert RcloneRemoteName.parse(value) is None


def test_rclone_remote_name_is_frozen():
    remote = RcloneRemoteName("archive")

    with pytest.raises(FrozenInstanceError):
        remote.value = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("decoded", "rooted", "components"),
    (
        ("base", False, ("base",)),
        ("base/nested", False, ("base", "nested")),
        ("/base", True, ("base",)),
        ("/base/nested", True, ("base", "nested")),
        ("/", True, ()),
        (
            "/Müller/experiment 1/a+b/@sample",
            True,
            ("Müller", "experiment 1", "a+b", "@sample"),
        ),
    ),
)
def test_registered_rclone_root_preserves_decoded_components_and_rootedness(
    decoded: str,
    rooted: bool,
    components: tuple[str, ...],
):
    root = RegisteredRcloneRoot.parse_decoded(decoded)

    assert root == RegisteredRcloneRoot(rooted=rooted, components=components)


@pytest.mark.parametrize(
    "decoded",
    (
        None,
        "",
        "//",
        "//base",
        "base/",
        "/base/",
        "base//nested",
        "/base//nested",
        ".",
        "..",
        "a/./b",
        "a/../b",
        "%",
        "%2e",
        "%2e%2e",
        r"back\slash",
        r"C:\secret",
        "C:/secret",
        r"\\server\share",
        r"\\?\C:\secret",
        r"\\.\NUL",
        "name:stream",
        "CON",
        "NUL.txt",
        "trailing.",
        "trailing ",
        "query?name",
        "fragment#name",
        "nul\x00byte",
        "line\nbreak",
        "..／secret",
        "．．/secret",
        "nested＼secret",
        "name：stream",
        "\ud800",
    ),
)
def test_registered_rclone_root_rejects_traversal_and_portable_aliases(
    decoded: object,
):
    assert RegisteredRcloneRoot.parse_decoded(decoded) is None


def test_registered_rclone_root_is_frozen_and_rejects_empty_relative_root():
    root = RegisteredRcloneRoot(True, ("base",))

    with pytest.raises(FrozenInstanceError):
        root.rooted = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        root.components = ("changed",)  # type: ignore[misc]
    with pytest.raises(ValueError, match="root is invalid"):
        RegisteredRcloneRoot(False, ())
    with pytest.raises(ValueError, match="root is invalid"):
        RegisteredRcloneRoot(True, ["base"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("root_text", "expected"),
    (
        ("base", "Lab Drive+archive@org:base/測定/a+b/@sample.bin"),
        ("/base", "Lab Drive+archive@org:/base/測定/a+b/@sample.bin"),
        ("/", "Lab Drive+archive@org:/測定/a+b/@sample.bin"),
    ),
)
def test_registered_rclone_root_composes_one_exact_decoded_target(
    root_text: str,
    expected: str,
):
    remote = RcloneRemoteName.parse("Lab Drive+archive@org")
    root = RegisteredRcloneRoot.parse_decoded(root_text)
    locator = PortableStorePath.parse_decoded("測定/a+b/@sample.bin")
    assert remote is not None
    assert root is not None
    assert locator is not None

    assert root.compose(remote, locator) == expected


def test_rooted_and_relative_rclone_targets_remain_structurally_distinct():
    remote = RcloneRemoteName("archive")
    relative = RegisteredRcloneRoot.parse_decoded("base")
    rooted = RegisteredRcloneRoot.parse_decoded("/base")
    locator = PortableStorePath(("artifact.bin",))
    assert relative is not None
    assert rooted is not None

    assert relative != rooted
    assert relative.compose(remote, locator) == "archive:base/artifact.bin"
    assert rooted.compose(remote, locator) == "archive:/base/artifact.bin"


def test_registered_rclone_root_enforces_combined_canonical_path_budget():
    at_limit_root_text = "/".join([*("a" * 255 for _ in range(15)), "b" * 254])
    over_after_composition_root_text = "/".join(
        [*("a" * 255 for _ in range(15)), "b" * 254, "c"]
    )
    at_limit_root = RegisteredRcloneRoot.parse_decoded(at_limit_root_text)
    over_after_composition_root = RegisteredRcloneRoot.parse_decoded(
        over_after_composition_root_text
    )
    remote = RcloneRemoteName("archive")
    locator = PortableStorePath(("x",))
    assert at_limit_root is not None
    assert over_after_composition_root is not None

    at_limit_target = at_limit_root.compose(remote, locator)

    assert at_limit_target is not None
    assert over_after_composition_root.compose(remote, locator) is None


def test_registered_rclone_root_compose_rejects_untyped_inputs():
    root = RegisteredRcloneRoot.parse_decoded("base")
    remote = RcloneRemoteName("archive")
    locator = PortableStorePath(("x",))
    assert root is not None

    assert root.compose("archive", locator) is None  # type: ignore[arg-type]
    assert root.compose(remote, "x") is None  # type: ignore[arg-type]
