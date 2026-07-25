from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from lab_tracker.git_remote_policy import (
    ApprovedGitRemote,
    GitPathStyle,
    GitRemotePolicy,
)


def test_policy_defaults_to_deny_all():
    assert GitRemotePolicy.deny_all().authorize("https://git.example/repo.git") is None
    assert GitRemotePolicy.from_config(None).authorize("https://git.example/repo.git") is None
    assert GitRemotePolicy.from_config("").authorize("https://git.example/repo.git") is None


def test_policy_returns_a_frozen_canonical_value():
    approved = GitRemotePolicy.from_config(
        "https://xn--bcher-kva.example/Org"
    ).authorize("HTTPS://BÜCHER.Example:443/Org/Repo.git")

    assert approved is not None
    assert approved.scheme == "https"
    assert approved.host == "xn--bcher-kva.example"
    assert approved.effective_port == 443
    assert approved.ssh_user is None
    assert approved.path_style is GitPathStyle.URL
    assert approved.path_segments == ("Org", "Repo.git")
    assert approved.subprocess_value == "https://xn--bcher-kva.example/Org/Repo.git"
    with pytest.raises(FrozenInstanceError):
        approved.host = "evil.example"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("grant", "candidate", "canonical"),
    [
        (
            "https://git.example/org",
            "https://GIT.EXAMPLE:443/org/repo.git",
            "https://git.example/org/repo.git",
        ),
        (
            "https://192.0.2.10/org",
            "https://192.0.2.10:443/org/repo.git",
            "https://192.0.2.10/org/repo.git",
        ),
        (
            "https://[2001:db8::1]/org",
            "https://[2001:0db8:0:0::1]:443/org/repo.git",
            "https://[2001:db8::1]/org/repo.git",
        ),
        (
            "ssh://git@git.example:22/org",
            "ssh://git@GIT.EXAMPLE/org/repo.git",
            "ssh://git@git.example/org/repo.git",
        ),
        (
            "ssh://git@git.example:2222/org",
            "ssh://git@git.example:2222/org/repo.git",
            "ssh://git@git.example:2222/org/repo.git",
        ),
        (
            "git://git.example:9418/org",
            "git://GIT.EXAMPLE/org/repo.git",
            "git://git.example/org/repo.git",
        ),
        (
            "git@git.example:org",
            "git@GIT.EXAMPLE:org/repo.git",
            "git@git.example:org/repo.git",
        ),
        (
            "git@git.example:/org",
            "git@GIT.EXAMPLE:/org/repo.git",
            "git@git.example:/org/repo.git",
        ),
    ],
)
def test_supported_forms_normalize_and_authorize(grant, candidate, canonical):
    approved = GitRemotePolicy.from_config(grant).authorize(candidate)

    assert approved is not None
    assert approved.subprocess_value == canonical


def test_host_root_grant_is_segment_prefix_and_canonicalizes_a_slash():
    policy = GitRemotePolicy.from_config("https://git.example")

    root = policy.authorize("https://git.example/")
    child = policy.authorize("https://git.example/org/repo.git")

    assert root is not None
    assert root.subprocess_value == "https://git.example/"
    assert child is not None


@pytest.mark.parametrize(
    "candidate",
    [
        "https://git.example.evil/org/repo.git",
        "https://git.example:444/org/repo.git",
        "ssh://git@git.example/org/repo.git",
        "git://git.example/org/repo.git",
        "https://git.example/organization/repo.git",
        "https://git.example/org2/repo.git",
    ],
)
def test_policy_matches_transport_origin_and_path_segments_exactly(candidate):
    policy = GitRemotePolicy.from_config("https://git.example/org")

    assert policy.authorize(candidate) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "ssh://root@git.example/org/repo.git",
        "ssh://git@git.example:2222/org/repo.git",
        "git@git.example:org/repo.git",
        "git@git.example:/org/repo.git",
    ],
)
def test_ssh_user_port_and_path_style_are_exact(candidate):
    policy = GitRemotePolicy.from_config("ssh://git@git.example/org")

    assert policy.authorize(candidate) is None


def test_scp_relative_and_absolute_grants_are_distinct():
    relative = GitRemotePolicy.from_config("git@git.example:org")
    absolute = GitRemotePolicy.from_config("git@git.example:/org")

    assert relative.authorize("git@git.example:org/repo.git") is not None
    assert relative.authorize("git@git.example:/org/repo.git") is None
    assert absolute.authorize("git@git.example:/org/repo.git") is not None
    assert absolute.authorize("git@git.example:org/repo.git") is None


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "http://git.example/org/repo.git",
        "ftp://git.example/org/repo.git",
        "file:///tmp/repo.git",
        "ext::sh -c evil",
        "https:://git.example/org/repo.git",
        "/tmp/repo.git",
        "./repo.git",
        "../repo.git",
        "~/repo.git",
        r"C:\repo.git",
        "C:/repo.git",
        "C:repo.git",
        "repo.git",
    ],
)
def test_unsupported_local_helper_and_drive_forms_are_rejected(candidate):
    policy = GitRemotePolicy.from_config("https://git.example/")

    assert policy.authorize(candidate) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "https://user@git.example/org/repo.git",
        "https://user:secret@git.example/org/repo.git",
        "git://user@git.example/org/repo.git",
        "ssh://git:secret@git.example/org/repo.git",
        "ssh://@git.example/org/repo.git",
        "git@@git.example:org/repo.git",
    ],
)
def test_credentials_and_malformed_users_are_rejected(candidate):
    policy = GitRemotePolicy.from_config("https://git.example/")

    assert policy.authorize(candidate) is None


@pytest.mark.parametrize(
    "candidate",
    [
        " https://git.example/org/repo.git",
        "https://git.example/org/repo.git ",
        "https://git.example/org/repo git",
        "https://git.example/org/\trepo.git",
        "https://git.example/org/\nrepo.git",
        "https://git.example/org/\x00repo.git",
        "https://git.example/org/\u200brepo.git",
        r"https://git.example\org\repo.git",
        "https://git.example/org%2Frepo.git",
        "https://git.example/org/repo%2Egit",
        "https://git.example/org/repo=main.git",
        "https://git.example/org/repo,main.git",
        "https://git.example/org/repo:main.git",
        "https://git.example/org/repo;main.git",
        "https://git.example/org/repo$main.git",
        "https://git.example/org/repo[main].git",
        "https://git.example/org/répôt.git",
        "https://git.example/org/repo.git?token=secret",
        "https://git.example/org/repo.git#main",
    ],
)
def test_ambiguous_or_invisible_characters_are_rejected(candidate):
    policy = GitRemotePolicy.from_config("https://git.example/")

    assert policy.authorize(candidate) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "--upload-pack=evil",
        "ssh://-git@git.example/org/repo.git",
        "ssh://git@-git.example/org/repo.git",
        "https://git.example/org/-upload-pack",
        "-git@git.example:org/repo.git",
    ],
)
def test_option_shaped_components_are_rejected(candidate):
    policy = GitRemotePolicy.from_config("https://git.example/")

    assert policy.authorize(candidate) is None


@pytest.mark.parametrize(
    "candidate",
    [
        "https://",
        "https:///org/repo.git",
        "https://git.example:/org/repo.git",
        "https://git.example:notaport/org/repo.git",
        "https://git.example:0/org/repo.git",
        "https://git.example:65536/org/repo.git",
        "https://2001:db8::1/org/repo.git",
        "https://[git.example]/org/repo.git",
        "https://git..example/org/repo.git",
        "https://-git.example/org/repo.git",
        "https://git.example-/org/repo.git",
        "https://127.1/org/repo.git",
        "https://2130706433/org/repo.git",
        "https://0x7f000001/org/repo.git",
        "https://0x7f.0x0.0x0.0x1/org/repo.git",
        "https://①②⑦.0.0.1/org/repo.git",
        "https://git.example/org//repo.git",
        "https://git.example/org/repo.git/",
        "https://git.example/org/./repo.git",
        "https://git.example/org/../repo.git",
        "[2001:db8::1]:org/repo.git",
        "git@[2001:db8::1]:org/repo.git",
        "git.example:",
    ],
)
def test_malformed_authorities_hosts_and_paths_are_rejected(candidate):
    policy = GitRemotePolicy.from_config("https://git.example/")

    assert policy.authorize(candidate) is None


def test_semantically_duplicate_grants_fail_without_echoing_values():
    with pytest.raises(ValueError) as captured:
        GitRemotePolicy.from_config(
            "HTTPS://BÜCHER.example:443/Org,"
            "https://xn--bcher-kva.example/Org",
            variable="TEST_GIT_REMOTES",
        )

    message = str(captured.value)
    assert message == (
        "TEST_GIT_REMOTES entry 2 is invalid (duplicate normalized grant)."
    )
    assert "BÜCHER" not in message
    assert "xn--" not in message


@pytest.mark.parametrize(
    ("raw", "index", "category"),
    [
        ("https://git.example/repo,", 2, "empty entry"),
        (",https://git.example/repo", 1, "empty entry"),
        ("https://git.example/repo,,ssh://git@git.example/repo", 2, "empty entry"),
        (" https://git.example/repo", 1, "whitespace or control characters"),
        ("https://git.example/repo ", 1, "whitespace or control characters"),
    ],
)
def test_config_errors_are_indexed_and_sanitized(raw, index, category):
    with pytest.raises(ValueError) as captured:
        GitRemotePolicy.from_config(raw, variable="TEST_GIT_REMOTES")

    assert str(captured.value) == (
        f"TEST_GIT_REMOTES entry {index} is invalid ({category})."
    )
    assert "git.example" not in str(captured.value)


def test_config_error_does_not_echo_embedded_secret_or_host():
    raw = "https://user:top-secret@private.example/repo.git"

    with pytest.raises(ValueError) as captured:
        GitRemotePolicy.from_config(raw, variable="TEST_GIT_REMOTES")

    message = str(captured.value)
    assert message == (
        "TEST_GIT_REMOTES entry 1 is invalid (embedded credentials)."
    )
    assert "top-secret" not in message
    assert "private.example" not in message


def test_normalized_root_and_default_port_are_duplicate_grants():
    with pytest.raises(ValueError, match="entry 2.*duplicate normalized grant"):
        GitRemotePolicy.from_config(
            "https://git.example,https://GIT.EXAMPLE:443/"
        )


def test_trailing_dns_dot_is_rejected_instead_of_collapsed():
    policy = GitRemotePolicy.from_config("https://git.internal/org")

    assert policy.authorize("https://git.internal./org/repo.git") is None
    with pytest.raises(ValueError, match="entry 1.*malformed hostname"):
        GitRemotePolicy.from_config("https://git.internal./org")


def test_strict_idna_keeps_sharp_s_distinct_from_ascii_ss():
    unicode_policy = GitRemotePolicy.from_config("https://faß.de/org")
    ascii_policy = GitRemotePolicy.from_config("https://fass.de/org")

    unicode_remote = unicode_policy.authorize("https://xn--fa-hia.de/org/repo.git")
    assert unicode_remote is not None
    assert unicode_remote.subprocess_value == "https://xn--fa-hia.de/org/repo.git"
    assert unicode_policy.authorize("https://fass.de/org/repo.git") is None
    assert ascii_policy.authorize("https://faß.de/org/repo.git") is None


@pytest.mark.parametrize(
    "candidate",
    [
        "https://℡.example/org/repo.git",
        "https://①②⑦.0.0.1/org/repo.git",
    ],
)
def test_idna_compatibility_mappings_fail_closed(candidate):
    policy = GitRemotePolicy.from_config("https://tel.example/")

    assert policy.authorize(candidate) is None


def test_grants_and_approved_values_are_immutable():
    policy = GitRemotePolicy.from_config("https://git.example/org")
    approved = policy.authorize("https://git.example/org/repo.git")

    assert isinstance(approved, ApprovedGitRemote)
    with pytest.raises(FrozenInstanceError):
        policy._grants = ()  # type: ignore[misc]
