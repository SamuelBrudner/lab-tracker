from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.rclone_remote_policy import RcloneRemotePolicy
from lab_tracker.rclone_store_locator import (
    RcloneRemoteName,
    RegisteredRcloneRoot,
)


def test_policy_defaults_to_deny_all():
    for policy in (
        RcloneRemotePolicy.deny_all(),
        RcloneRemotePolicy.from_config(None),
        RcloneRemotePolicy.from_config(""),
    ):
        assert policy.authorize("archive") is None


def test_policy_authorizes_only_exact_case_and_unicode_spelling():
    policy = RcloneRemotePolicy.from_config("Archive,Kelvin")

    archive = policy.authorize("Archive")
    compatibility_spelling = policy.authorize("Kelvin")

    assert archive == RcloneRemoteName("Archive")
    assert compatibility_spelling == RcloneRemoteName("Kelvin")
    assert policy.authorize("archive") is None
    assert policy.authorize("Kelvin") is None


def test_compatibility_equivalent_names_remain_distinct_exact_grants():
    policy = RcloneRemotePolicy.from_config("Kelvin,Kelvin")

    assert policy.authorize("Kelvin") == RcloneRemoteName("Kelvin")
    assert policy.authorize("Kelvin") == RcloneRemoteName("Kelvin")


def test_authorize_name_preserves_the_typed_candidate():
    policy = RcloneRemotePolicy.from_config("Lab Drive")
    candidate = RcloneRemoteName("Lab Drive")

    assert policy.authorize_name(candidate) is candidate
    assert policy.authorize_name(RcloneRemoteName("lab drive")) is None
    assert policy.authorize_name("Lab Drive") is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "index", "category"),
    (
        (",archive", 1, "empty entry"),
        ("archive,", 2, "empty entry"),
        ("archive,,backup", 2, "empty entry"),
        (" archive", 1, "invalid remote name"),
        ("archive ", 1, "invalid remote name"),
        ("archive,archive", 2, "duplicate exact grant"),
    ),
)
def test_config_errors_are_indexed_and_redacted(
    raw: str,
    index: int,
    category: str,
):
    with pytest.raises(ValueError) as captured:
        RcloneRemotePolicy.from_config(
            raw,
            variable="TEST_RCLONE_REMOTES",
        )

    assert str(captured.value) == (f"TEST_RCLONE_REMOTES entry {index} is invalid ({category}).")
    assert "archive" not in str(captured.value).lower()


def test_invalid_config_does_not_echo_operator_content():
    secret = "private/remote-with-secret"

    with pytest.raises(ValueError) as captured:
        RcloneRemotePolicy.from_config(
            secret,
            variable="TEST_RCLONE_REMOTES",
        )

    assert str(captured.value) == ("TEST_RCLONE_REMOTES entry 1 is invalid (invalid remote name).")
    assert secret not in str(captured.value)


def test_nfkc_unsafe_name_is_rejected_by_config_and_authorization():
    # U+1F101 DIGIT ZERO COMMA is itself a Unicode number, but NFKC expands it
    # to ``0,``. The comma would become a configuration delimiter.
    unsafe_name = "\U0001f101"

    with pytest.raises(
        ValueError,
        match=r"^TEST_RCLONE_REMOTES entry 1 is invalid "
        r"\(invalid remote name\)\.$",
    ):
        RcloneRemotePolicy.from_config(
            unsafe_name,
            variable="TEST_RCLONE_REMOTES",
        )

    assert RcloneRemotePolicy.deny_all().authorize(unsafe_name) is None


def test_policy_and_approved_name_are_frozen():
    policy = RcloneRemotePolicy.from_config("archive")
    approved = policy.authorize("archive")

    assert approved is not None
    with pytest.raises(FrozenInstanceError):
        policy._grants = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        approved.value = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("root_text", "expected"),
    (
        ("path", "archive:path"),
        ("/path", "archive:/path"),
        ("/", "archive:/"),
    ),
)
def test_registered_root_composes_itself_without_string_surgery(
    root_text: str,
    expected: str,
):
    root = RegisteredRcloneRoot.parse_decoded(root_text)
    remote = RcloneRemotePolicy.from_config("archive").authorize("archive")
    assert root is not None
    assert remote is not None

    assert root.compose_root(remote) == expected


def test_registered_root_rejects_untyped_remote_for_root_composition():
    root = RegisteredRcloneRoot.parse_decoded("/path")
    assert root is not None

    assert root.compose_root("archive") is None  # type: ignore[arg-type]
