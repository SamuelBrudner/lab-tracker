import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import lab_tracker.local_path_policy as local_path_policy
from lab_tracker.local_path_policy import (
    LocalPathPolicy,
    is_supported_absolute_local_root,
    native_local_path_from_uri,
)


def _uri_with_authority(path: Path, authority: str) -> str:
    native_uri = path.as_uri()
    assert native_uri.startswith("file://")
    return f"file://{authority}{native_uri.removeprefix('file://')}"


@pytest.mark.parametrize(
    "name",
    (
        "artifact with spaces.txt",
        "artifact%result.txt",
        "artifact#fragment.txt",
        "literal%2Fseparator.bin",
        "résult-数据.txt",
    ),
)
def test_native_path_as_uri_round_trips(tmp_path, name):
    path = tmp_path / name

    assert native_local_path_from_uri(path.as_uri()) == str(path)


@pytest.mark.skipif(os.name == "nt", reason="'?' is not a valid Windows filename")
def test_posix_question_mark_path_as_uri_round_trips(tmp_path):
    path = tmp_path / "artifact?query.txt"

    assert native_local_path_from_uri(path.as_uri()) == str(path)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX filesystem bytes")
def test_posix_non_utf8_path_as_uri_round_trips_with_surrogateescape(tmp_path):
    raw_path = os.fsencode(tmp_path) + b"/artifact-\xff.bin"
    path = Path(os.fsdecode(raw_path))

    assert native_local_path_from_uri(path.as_uri()) == str(path)


def test_native_absolute_path_is_accepted_without_uri_decoding(tmp_path):
    path = tmp_path / "literal%2Fname.bin"

    assert native_local_path_from_uri(str(path)) == str(path)


def test_relative_native_path_is_rejected():
    assert native_local_path_from_uri("relative/artifact.bin") is None


@pytest.mark.parametrize("authority", ("", "localhost", "LOCALHOST", "LoCaLhOsT"))
def test_empty_or_localhost_authority_is_accepted(tmp_path, authority):
    path = tmp_path / "authority.txt"

    assert native_local_path_from_uri(_uri_with_authority(path, authority)) == str(path)


@pytest.mark.parametrize(
    "authority",
    (
        "fileserver.example",
        "127.0.0.1",
        "[::1]",
        "%6cocalhost",
        "localhost.",
        "user@localhost",
        "user:password@localhost",
        "localhost:80",
    ),
)
def test_nonlocal_authorities_are_rejected_before_canonicalization(
    tmp_path, authority, monkeypatch
):
    path = tmp_path / "authority-secret.txt"
    policy = LocalPathPolicy()

    def unexpected_realpath(_path):
        raise AssertionError("rejected URI reached filesystem canonicalization")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert policy.authorize_uri(_uri_with_authority(path, authority)) is None


@pytest.mark.parametrize(
    "uri",
    (
        "file:////fileserver.example/share/artifact.bin",
        "file:relative/artifact.bin",
        "file:///tmp/artifact%00.bin",
        "file:///tmp/artifact%1F.bin",
        "file:///tmp/artifact%7F.bin",
        "file:///tmp/bad%ZZescape.bin",
        "file:///tmp/trailing%.bin",
        "file:///tmp/raw space.bin",
        "file:///tmp/encoded%2Fseparator.bin",
        "file:///tmp/artifact.bin?download=1",
        "file:///tmp/artifact.bin#preview",
    ),
)
def test_malformed_or_ambiguous_file_uris_are_rejected(uri):
    assert native_local_path_from_uri(uri) is None


def test_double_encoded_separator_is_decoded_exactly_once(tmp_path):
    path = tmp_path / "literal%2Fseparator.bin"

    assert native_local_path_from_uri(path.as_uri()) == str(path)


def test_empty_root_policy_denies_without_candidate_canonicalization(tmp_path, monkeypatch):
    policy = LocalPathPolicy([])

    def unexpected_realpath(_path):
        raise AssertionError("deny-all policy touched the candidate filesystem")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert policy.authorize_path(tmp_path / "artifact.bin") is None


def test_policy_is_frozen_slotted_and_uses_identity_semantics() -> None:
    policy = LocalPathPolicy([])
    equivalent = LocalPathPolicy([])

    assert policy is not equivalent
    assert policy != equivalent
    assert not hasattr(policy, "__dict__")
    with pytest.raises(FrozenInstanceError):
        policy._canonical_roots = None  # type: ignore[misc]


@pytest.mark.parametrize("raw", (None, "", " \t "))
def test_configured_empty_roots_are_deny_all(raw: str | None) -> None:
    policy = LocalPathPolicy.from_config(raw)

    assert policy.canonical_roots == ()
    assert policy.lexical_roots == ()
    assert policy.recovery_roots == ()


def test_configured_roots_preserve_pathsep_parsing(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    raw = os.pathsep.join((str(first), "", str(second)))

    policy = LocalPathPolicy.from_config(raw)

    assert policy.canonical_roots == (
        os.path.realpath(first),
        os.path.realpath(second),
    )
    assert policy.lexical_roots == (str(first), str(second))
    assert policy.recovery_roots == (str(first), str(second))


def test_configured_roots_reject_non_string_values() -> None:
    with pytest.raises(TypeError, match="must be a string"):
        LocalPathPolicy.from_config(1)  # type: ignore[arg-type]


def test_configured_root_normalization_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "operator-root-secret"

    def fail_expansion(_path: Path) -> Path:
        raise RuntimeError(secret)

    monkeypatch.setattr(Path, "expanduser", fail_expansion)

    with pytest.raises(ValueError) as exc_info:
        LocalPathPolicy.from_config("~/private")

    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    "root",
    (
        "relative/store",
        "~/store",
        "C:store",
        r"\\server\share",
        r"\\?\C:\store",
        r"\\.\PhysicalDrive0",
    ),
)
def test_registered_root_predicate_rejects_non_native_absolute_root_without_io(root, monkeypatch):
    def unexpected_path_operation(_path):
        raise AssertionError("invalid raw root reached a host-path operation")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_path_operation)
    monkeypatch.setattr(local_path_policy.os.path, "abspath", unexpected_path_operation)

    assert is_supported_absolute_local_root(root) is False


def test_operator_policy_preserves_relative_and_tilde_root_normalization(tmp_path, monkeypatch):
    relative_root = tmp_path / "relative-store"
    tilde_root = tmp_path / "tilde-store"
    relative_root.mkdir()
    tilde_root.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    relative_policy = LocalPathPolicy(["relative-store"])
    tilde_policy = LocalPathPolicy(["~/tilde-store"])

    assert relative_policy.canonical_roots == (os.path.realpath(relative_root),)
    assert tilde_policy.canonical_roots == (os.path.realpath(tilde_root),)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symbolic links")
def test_operator_policy_preserves_link_parent_traversal_semantics(tmp_path):
    allowed = tmp_path / "allowed"
    physical_root = allowed / "narrow"
    link_target = physical_root / "nested"
    link_target.mkdir(parents=True)
    link = allowed / "link"
    try:
        link.symlink_to(link_target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symbolic links are unavailable: {exc}")
    configured_root = os.path.join(os.fspath(link), os.pardir)

    policy = LocalPathPolicy([configured_root])

    assert policy.canonical_roots == (os.path.realpath(configured_root),)
    assert policy.canonical_roots == (os.fspath(physical_root),)
    assert policy.canonical_roots != (os.fspath(allowed),)


def test_absolute_root_predicate_is_lexical_and_side_effect_free(tmp_path, monkeypatch):
    def unexpected_realpath(_path):
        raise AssertionError("root predicate canonicalized the filesystem")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert is_supported_absolute_local_root(tmp_path / "store") is True


def test_unscoped_policy_restricts_to_exact_store_root(tmp_path):
    store = tmp_path / "store"
    store.mkdir()

    restricted = LocalPathPolicy().restricted_to_absolute_root(store)

    assert restricted is not None
    assert restricted.canonical_roots == (os.path.realpath(store),)


def test_deny_all_policy_cannot_delegate_store_root(tmp_path):
    store = tmp_path / "store"
    store.mkdir()

    assert LocalPathPolicy([]).restricted_to_absolute_root(store) is None


def test_broad_operator_root_restricts_to_whole_store_root(tmp_path):
    operator_root = tmp_path / "allowed"
    store = operator_root / "registered-store"
    store.mkdir(parents=True)

    restricted = LocalPathPolicy([operator_root]).restricted_to_absolute_root(store)

    assert restricted is not None
    assert restricted.canonical_roots == (os.path.realpath(store),)


def test_equal_operator_and_store_root_is_authorized(tmp_path):
    store = tmp_path / "store"
    store.mkdir()

    restricted = LocalPathPolicy([store]).restricted_to_absolute_root(store)

    assert restricted is not None
    assert restricted.canonical_roots == (os.path.realpath(store),)


def test_narrow_operator_root_does_not_partially_authorize_broader_store(tmp_path):
    store = tmp_path / "store"
    granted_child = store / "granted"
    granted_child.mkdir(parents=True)

    assert LocalPathPolicy([granted_child]).restricted_to_absolute_root(store) is None


def test_disjoint_operator_and_store_roots_do_not_intersect(tmp_path, monkeypatch):
    operator_root = tmp_path / "allowed"
    store = tmp_path / "store"
    operator_root.mkdir()
    store.mkdir()
    policy = LocalPathPolicy([operator_root])

    def unexpected_realpath(_path):
        raise AssertionError("disjoint store root reached filesystem canonicalization")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert policy.restricted_to_absolute_root(store) is None


def test_sibling_prefix_is_not_contained(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    sibling = tmp_path / "store-private"
    sibling.mkdir()
    policy = LocalPathPolicy([root])

    assert policy.authorize_path(sibling / "secret.bin") is None


def test_encoded_dot_segments_cannot_escape_root(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    sibling = tmp_path / "private"
    sibling.mkdir()
    uri = f"{root.as_uri()}/nested/%2e%2e/%2e%2e/{sibling.name}/secret.bin"
    policy = LocalPathPolicy([root])

    assert policy.authorize_uri(uri) is None


def test_filesystem_anchor_contains_descendant(tmp_path):
    path = tmp_path / "nested" / "artifact.bin"
    policy = LocalPathPolicy([Path(path.anchor)])

    assert policy.authorize_path(path) == os.path.realpath(path)


def test_static_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    link = root / "linked-private"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    policy = LocalPathPolicy([root])

    assert policy.authorize_path(link / "secret.bin") is None


def test_canonical_roots_collapse_aliases_and_overlapping_children(tmp_path):
    root = tmp_path / "store"
    child = root / "nested"
    child.mkdir(parents=True)
    alias = tmp_path / "store-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    policy = LocalPathPolicy([alias, child, root])

    assert policy.canonical_roots == (os.path.realpath(root),)
    assert policy.lexical_roots == (str(alias), str(child), str(root))
    assert policy.recovery_roots == (str(alias),)


def test_walk_pruning_removes_linked_directory(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    link = root / "linked-private"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    children = [link.name]
    policy = LocalPathPolicy([root])

    policy.prune_walk_directories(str(root), children)

    assert children == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows drive paths")
def test_windows_drive_path_as_uri_round_trips(tmp_path):
    path = tmp_path / "artifact.bin"

    assert native_local_path_from_uri(path.as_uri()) == str(path)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path semantics")
@pytest.mark.parametrize(
    "relative_path",
    (
        "NUL",
        "CON.txt",
        "COM1",
        "LPT9.foo",
        "CONIN$",
        "directory./artifact.bin",
        "directory /artifact.bin",
        "artifact.bin:stream",
        "COM1/artifact.bin",
    ),
)
def test_windows_reserved_components_are_rejected_before_realpath(
    tmp_path, relative_path, monkeypatch
):
    root = tmp_path / "store"
    root.mkdir()
    policy = LocalPathPolicy([root])
    uri = (root / Path(relative_path)).as_uri()

    def unexpected_realpath(_path):
        raise AssertionError("reserved path reached filesystem canonicalization")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert policy.authorize_uri(uri) is None
    assert policy.authorize_path(root / Path(relative_path)) is None


@pytest.mark.skipif(os.name != "nt", reason="requires Windows file-URI grammar")
def test_windows_malformed_drive_aliases_and_raw_backslashes_are_rejected(tmp_path):
    path_part = (tmp_path / "artifact.bin").as_uri().removeprefix("file:///")
    drive, separator, remainder = path_part.partition("/")
    assert separator and "/" in remainder
    raw_remainder = remainder.replace("/", "\\", 1)
    raw_backslash_path = f"{drive}/{raw_remainder}"
    encoded_remainder = remainder.replace("/", "%5C", 1)
    encoded_backslash_path = f"{drive}/{encoded_remainder}"
    malformed = (
        f"file:///garbage{path_part}",
        f"file:////server/{path_part}",
        f"file:///{raw_backslash_path}",
        f"file:///{encoded_backslash_path}",
    )

    assert all(native_local_path_from_uri(uri) is None for uri in malformed)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows UNC paths")
def test_windows_native_unc_path_is_rejected_before_realpath(tmp_path, monkeypatch):
    policy = LocalPathPolicy([tmp_path])

    def unexpected_realpath(_path):
        raise AssertionError("native UNC path reached canonicalization")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert policy.authorize_path(r"\\fileserver.example\share\artifact.bin") is None


@pytest.mark.skipif(os.name != "nt", reason="requires Windows path semantics")
def test_windows_existing_path_case_variation_resolves_on_normal_volume(tmp_path):
    root = tmp_path / "MixedCaseStore"
    root.mkdir()
    path = root / "Artifact.BIN"
    path.write_bytes(b"x")
    differently_cased = Path(str(path).swapcase())
    if not differently_cased.is_file():
        pytest.skip("test volume is case-sensitive")
    policy = LocalPathPolicy([root])

    assert policy.authorize_path(differently_cased) == os.path.realpath(path)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows canonical semantics")
def test_windows_final_containment_does_not_fold_component_case(tmp_path):
    root = tmp_path / "CaseParent" / "Store"
    root.mkdir(parents=True)
    case_distinct_sibling = root.parent / "store" / "secret.bin"
    policy = LocalPathPolicy([root])

    assert policy.contains_canonical_path(str(case_distinct_sibling)) is False


@pytest.mark.skipif(os.name != "nt", reason="requires Windows drive anchors")
def test_windows_cross_drive_candidate_fails_before_realpath(tmp_path, monkeypatch):
    current_drive = Path(tmp_path).drive.upper()
    other_drive = next(drive for drive in ("Z:", "Y:", "X:", "W:") if drive != current_drive)
    policy = LocalPathPolicy([tmp_path])

    def unexpected_realpath(_path):
        raise AssertionError("cross-drive candidate reached canonicalization")

    monkeypatch.setattr(local_path_policy.os.path, "realpath", unexpected_realpath)

    assert policy.authorize_uri(Path(f"{other_drive}/artifact.bin").as_uri()) is None


def _create_windows_junction(junction: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, (
        f"mklink /J failed: stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_windows_policy_rejects_and_prunes_junction_escape(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    junction = root / "mounted-private"
    _create_windows_junction(junction, outside)
    policy = LocalPathPolicy([root])
    children = [junction.name]

    assert policy.authorize_path(junction / "secret.bin") is None
    policy.prune_walk_directories(str(root), children)
    assert children == []
