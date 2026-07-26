from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import lab_tracker.local_filesystem_authority as authority_module
from lab_tracker.local_filesystem_authority import (
    MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS,
    MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES,
    MAX_LOCAL_FILESYSTEM_PATH_BYTES,
    MAX_LOCAL_FILESYSTEM_PATH_CHARACTERS,
    MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS,
    LocalDirectoryGrant,
    LocalFilesystemAuthority,
)


def _selected_request(
    authority: LocalFilesystemAuthority,
    candidate: str | Path,
) -> tuple[str, tuple[str, ...]] | None:
    grant = authority.select_directory(candidate)
    return None if grant is None else authority._request_for(grant)


def test_unset_empty_and_whitespace_configurations_deny_all_without_getcwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_getcwd() -> str:
        raise AssertionError("deny-all authority captured a working directory")

    isolated_os = SimpleNamespace(**vars(os))
    isolated_os.getcwd = unexpected_getcwd
    monkeypatch.setattr(authority_module, "os", isolated_os)

    for raw in (None, "", " \t "):
        authority = LocalFilesystemAuthority.from_config(raw)
        assert authority.legacy_roots == ()
        assert authority.select_directory("/candidate") is None


def test_authority_is_slotted_frozen_identity_based_and_redacted(tmp_path: Path) -> None:
    secret = tmp_path / "operator-secret"
    authority = LocalFilesystemAuthority.from_roots([secret])
    equivalent = LocalFilesystemAuthority.from_roots([secret])

    assert authority is not equivalent
    assert authority != equivalent
    assert not hasattr(authority, "__dict__")
    assert str(secret) not in repr(authority)
    with pytest.raises(FrozenInstanceError):
        authority._roots = ()  # type: ignore[misc]

    grant = authority.select_directory(secret / "store")
    assert grant is not None
    assert not hasattr(grant, "__dict__")
    assert str(secret) not in repr(grant)


def test_selection_is_purely_lexical_and_never_calls_target_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    candidate = allowed / "alias" / "store"
    authority = LocalFilesystemAuthority.from_roots([allowed])

    def unexpected_filesystem_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("lexical authority touched the target filesystem")

    monkeypatch.setattr(
        authority_module,
        "os",
        SimpleNamespace(
            name=os.name,
            fspath=os.fspath,
            fsencode=os.fsencode,
            getcwd=unexpected_filesystem_call,
            open=unexpected_filesystem_call,
            stat=unexpected_filesystem_call,
            lstat=unexpected_filesystem_call,
            readlink=unexpected_filesystem_call,
        ),
    )

    assert _selected_request(authority, candidate) == (
        str(candidate),
        (str(allowed),),
    )


def test_relative_roots_use_one_captured_startup_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def recording_getcwd() -> str:
        nonlocal calls
        calls += 1
        return str(tmp_path)

    isolated_os = SimpleNamespace(**vars(os))
    isolated_os.getcwd = recording_getcwd
    monkeypatch.setattr(authority_module, "os", isolated_os)

    authority = LocalFilesystemAuthority.from_roots(["first", "second/nested"])

    assert calls == 1
    assert authority.legacy_roots == (
        str(tmp_path / "first"),
        str(tmp_path / "second" / "nested"),
    )


def test_configuration_preserves_path_list_parsing_and_omits_empty_parts(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    raw = os.pathsep.join((str(first), "", str(second)))

    authority = LocalFilesystemAuthority.from_config(raw)

    assert authority.legacy_roots == (str(first), str(second))


def test_explicit_captured_working_directory_is_stable_after_chdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup = tmp_path / "startup"
    later = tmp_path / "later"
    startup.mkdir()
    later.mkdir()
    authority = LocalFilesystemAuthority.from_roots(["stores"], cwd=startup)

    monkeypatch.chdir(later)

    assert authority.legacy_roots == (str(startup / "stores"),)
    assert _selected_request(authority, startup / "stores" / "one") == (
        str(startup / "stores" / "one"),
        (str(startup / "stores"),),
    )
    assert authority.select_directory(later / "stores" / "one") is None


def test_tilde_roots_retain_operator_configuration_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    authority = LocalFilesystemAuthority.from_roots(["~/stores"])

    assert authority.legacy_roots == (str(tmp_path / "stores"),)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX environment semantics")
def test_tilde_expansion_is_environment_only_and_never_uses_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOME", raising=False)

    def unexpected_expanduser(_root: str) -> str:
        raise AssertionError("authority delegated tilde expansion to NSS-capable code")

    monkeypatch.setattr(authority_module.posixpath, "expanduser", unexpected_expanduser)

    with pytest.raises(ValueError, match="valid local paths"):
        LocalFilesystemAuthority.from_roots(["~/stores"])


def test_named_user_tilde_roots_are_rejected_without_lookup() -> None:
    with pytest.raises(ValueError, match="valid local paths"):
        LocalFilesystemAuthority.from_roots(["~another-user/stores"])


@pytest.mark.parametrize(
    "root",
    (
        "link/..",
        "./store",
        "nested/./store",
        "nested//store",
        "bad\0root",
        "bad\nroot",
    ),
)
def test_ambiguous_or_malformed_operator_roots_are_rejected_without_identity(
    root: str,
    tmp_path: Path,
) -> None:
    secret = f"operator-secret/{root}"

    with pytest.raises(ValueError) as exc_info:
        LocalFilesystemAuthority.from_roots([secret], cwd=tmp_path)

    rendered = f"{exc_info.value!s}\n{exc_info.value!r}"
    assert "operator-secret" not in rendered


def test_candidate_alias_parent_suffix_is_preserved_for_native_resolution(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    authority = LocalFilesystemAuthority.from_roots([allowed])
    candidate = f"{allowed}/link/.."

    assert _selected_request(authority, candidate) == (
        candidate,
        (str(allowed),),
    )


def test_candidate_that_provably_pops_above_root_is_denied_without_collapsing(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    authority = LocalFilesystemAuthority.from_roots([allowed])

    assert authority.select_directory(f"{allowed}/../escape") is None
    assert authority.select_directory(f"{allowed}/./../escape") is None
    assert authority.select_directory(f"{allowed}/link/../../escape") is not None


def test_component_containment_rejects_sibling_prefix(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    sibling = tmp_path / "allowed-secret"
    authority = LocalFilesystemAuthority.from_roots([allowed])

    assert authority.select_directory(sibling) is None
    assert authority.select_directory(allowed) is not None
    assert authority.select_directory(allowed / "child") is not None


def test_most_specific_containing_root_is_the_only_selected_grant(
    tmp_path: Path,
) -> None:
    broad = tmp_path / "allowed"
    narrow = broad / "project"
    authority = LocalFilesystemAuthority.from_roots([broad, narrow])
    candidate = narrow / "store"

    assert _selected_request(authority, candidate) == (
        str(candidate),
        (str(narrow),),
    )


def test_invalid_most_specific_selection_never_falls_back_to_broader_root(
    tmp_path: Path,
) -> None:
    broad = tmp_path / "allowed"
    narrow = broad / "project"
    authority = LocalFilesystemAuthority.from_roots([broad, narrow])

    assert authority.select_directory(f"{narrow}/../other") is None


def test_grants_are_bound_to_the_issuing_authority(tmp_path: Path) -> None:
    first = LocalFilesystemAuthority.from_roots([tmp_path])
    second = LocalFilesystemAuthority.from_roots([tmp_path])
    grant = first.select_directory(tmp_path / "store")
    assert grant is not None

    with pytest.raises(ValueError, match="grant is invalid"):
        second._request_for(grant)
    forged = LocalDirectoryGrant(object(), 0, str(tmp_path / "store"))
    with pytest.raises(ValueError, match="grant is invalid"):
        first._request_for(forged)


def test_duplicate_roots_preserve_one_legacy_identity(tmp_path: Path) -> None:
    authority = LocalFilesystemAuthority.from_roots([tmp_path, tmp_path])

    assert authority.legacy_roots == (str(tmp_path),)


def test_configured_authority_root_count_has_a_fixed_ceiling(tmp_path: Path) -> None:
    at_limit = [tmp_path / f"root-{index}" for index in range(MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS)]
    authority = LocalFilesystemAuthority.from_roots(at_limit)
    assert len(authority.legacy_roots) == MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS

    over_limit = [*at_limit, tmp_path / "one-too-many"]
    with pytest.raises(ValueError, match="valid local paths"):
        LocalFilesystemAuthority.from_roots(over_limit)


def test_root_iterables_are_consumed_only_to_the_fixed_ceiling(tmp_path: Path) -> None:
    consumed = 0

    def roots() -> object:
        nonlocal consumed
        for index in range(MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS + 1):
            consumed += 1
            yield tmp_path / f"root-{index}"
        raise AssertionError("authority consumed beyond its fixed ceiling")

    with pytest.raises(ValueError, match="valid local paths"):
        LocalFilesystemAuthority.from_roots(roots())  # type: ignore[arg-type]

    assert consumed == MAX_LOCAL_FILESYSTEM_AUTHORITY_ROOTS + 1


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX path semantics")
@pytest.mark.parametrize(
    "root",
    (
        "/" + "a" * (MAX_LOCAL_FILESYSTEM_PATH_CHARACTERS + 1),
        "/" + "é" * (MAX_LOCAL_FILESYSTEM_PATH_BYTES // 2 + 1),
        "/" + "/".join(["a"] * (MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS + 1)),
        "/" + "a" * (MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES + 1),
    ),
)
def test_root_admission_has_pre_split_size_and_component_ceilings(root: str) -> None:
    with pytest.raises(ValueError, match="valid local paths"):
        LocalFilesystemAuthority.from_roots([root])


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX path semantics")
def test_oversized_candidate_fails_closed_before_component_allocation() -> None:
    authority = LocalFilesystemAuthority.from_roots(["/allowed"])
    candidate = "/allowed/" + "/".join(
        ["a"] * (MAX_LOCAL_FILESYSTEM_PATH_COMPONENTS + 1)
    )

    assert authority.select_directory(candidate) is None


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX path semantics")
def test_oversized_candidate_component_is_denied_during_lexical_admission() -> None:
    authority = LocalFilesystemAuthority.from_roots(["/allowed"])
    candidate = "/allowed/" + "a" * (MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES + 1)

    assert authority.select_directory(candidate) is None


def test_non_string_configuration_and_non_sequence_roots_are_rejected() -> None:
    with pytest.raises(TypeError, match="configuration must be a string"):
        LocalFilesystemAuthority.from_config(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="sequence"):
        LocalFilesystemAuthority.from_roots("/tmp")  # type: ignore[arg-type]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX path semantics")
def test_posix_double_slash_namespace_is_unsupported() -> None:
    with pytest.raises(ValueError):
        LocalFilesystemAuthority.from_roots(["//host/share"])
    assert (
        LocalFilesystemAuthority.from_roots(["/allowed"]).select_directory("//host/share") is None
    )


def test_windows_candidate_lexical_admission_rejects_ambiguous_names() -> None:
    invalid = (
        r"\\server\share",
        r"\\?\C:\store",
        r"\\.\PhysicalDrive0",
        r"C:relative",
        r"C:\Allowed\artifact:stream",
        r"C:\Allowed\trailing.",
        "C:\\Allowed\\trailing ",
        r"C:\Allowed\CON",
        "C:\\Allowed\\bad\0name",
        "C:\\Allowed\\" + "a" * (MAX_LOCAL_FILESYSTEM_COMPONENT_BYTES + 1),
    )

    for candidate in invalid:
        with pytest.raises(ValueError):
            authority_module._parse_windows_candidate(candidate)

    parsed = authority_module._parse_windows_candidate(r"c:\Allowed\junction\..\store")
    assert parsed.rendered == r"C:\Allowed\junction\..\store"
    assert parsed.anchor == "C:"
    assert parsed.components == ("Allowed", "junction", "..", "store")


@pytest.mark.parametrize(
    ("candidate", "rendered", "components"),
    (
        (r"c:/Allowed/store", r"C:\Allowed\store", ("Allowed", "store")),
        ("c:\\Allowed\\store\\", r"C:\Allowed\store", ("Allowed", "store")),
        (r"c:\Allowed\\store", r"C:\Allowed\store", ("Allowed", "store")),
    ),
)
def test_windows_candidate_normalizes_only_safe_separator_aliases(
    candidate: str,
    rendered: str,
    components: tuple[str, ...],
) -> None:
    parsed = authority_module._parse_windows_candidate(candidate)

    assert parsed.rendered == rendered
    assert parsed.anchor == "C:"
    assert parsed.components == components


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows path semantics")
def test_windows_normalizes_safe_root_spelling_and_compares_components_exactly() -> None:
    authority = LocalFilesystemAuthority.from_roots(["c:/Allowed//"])

    assert authority.legacy_roots == (r"C:\Allowed",)
    request = _selected_request(authority, "C:\\Allowed\\store\\")
    assert request == (r"C:\Allowed\store", (r"C:\Allowed",))
    assert authority.select_directory(r"c:\allowed\store") is None


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows path semantics")
@pytest.mark.parametrize(
    "root",
    (
        r"\\server\share",
        r"\\?\C:\store",
        r"\\.\PhysicalDrive0",
        r"C:relative",
        r"C:\store\..",
        r"C:\CON",
        r"C:\trailing.",
    ),
)
def test_windows_unsupported_namespaces_and_components_fail_closed(root: str) -> None:
    with pytest.raises(ValueError):
        LocalFilesystemAuthority.from_roots([root])
