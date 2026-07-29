from __future__ import annotations

import builtins
import ntpath
import os
import socket
import subprocess
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

import lab_tracker.local_path_policy as local_path_policy
import lab_tracker.outbound_http as outbound_http
from lab_tracker.data_store_definition import (
    DATA_STORE_CREDENTIAL_REF_MAX_LENGTH,
    DATA_STORE_ENDPOINT_MAX_LENGTH,
    DATA_STORE_NAME_MAX_LENGTH,
    DATA_STORE_ROOT_MAX_LENGTH,
    DataStoreDefinitionError,
    DataStoreDefinitionErrorCode,
    ValidatedDataStoreDefinition,
)
from lab_tracker.db_models import DataStoreModel
from lab_tracker.models import StoreKind
from lab_tracker.rclone_store_definition import RCLONE_BACKED_STORE_KINDS


def _assert_error(
    code: DataStoreDefinitionErrorCode,
    **overrides: object,
) -> DataStoreDefinitionError:
    arguments: dict[str, object] = {
        "name": "analysis",
        "kind": StoreKind.HTTP,
        "root": "https://example.com/data/",
    }
    arguments.update(overrides)
    with pytest.raises(DataStoreDefinitionError) as captured:
        ValidatedDataStoreDefinition.create(**arguments)
    assert captured.value.code is code
    return captured.value


def _http_root_with_length(length: int) -> str:
    base = "https://example.com/"
    remaining = length - len(base)
    assert remaining > 0
    components: list[str] = []
    while remaining > 101:
        components.append("a" * 100)
        remaining -= 101
    components.append("b" * remaining)
    result = base + "/".join(components)
    assert len(result) == length
    return result


def _portable_path_with_length(length: int, *, rooted: bool) -> str:
    prefix = "/" if rooted else ""
    remaining = length - len(prefix)
    assert remaining > 0
    components: list[str] = []
    while remaining > 101:
        components.append("a" * 100)
        remaining -= 101
    components.append("b" * remaining)
    result = prefix + "/".join(components)
    assert len(result) == length
    return result


def test_definition_is_frozen_slotted_and_factory_only(tmp_path: object) -> None:
    root = os.fspath(tmp_path)
    definition = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.LOCAL_FS,
        root=root,
    )

    assert definition == ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.LOCAL_FS,
        root=root,
    )
    assert not hasattr(definition, "__dict__")
    with pytest.raises(FrozenInstanceError):
        definition.name = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="validated factory"):
        ValidatedDataStoreDefinition(
            "analysis",
            StoreKind.LOCAL_FS,
            root,
            None,
            None,
            _factory_token=object(),
        )


def test_stable_error_messages_are_static() -> None:
    expected_messages = {
        DataStoreDefinitionErrorCode.INVALID_KIND: "Data store kind is invalid.",
        DataStoreDefinitionErrorCode.UNSUPPORTED_KIND: "Data store kind is not supported.",
        DataStoreDefinitionErrorCode.INVALID_NAME: "Data store name is invalid.",
        DataStoreDefinitionErrorCode.NAME_TOO_LONG: "Data store name is too long.",
        DataStoreDefinitionErrorCode.INVALID_ROOT: "Data store root is invalid.",
        DataStoreDefinitionErrorCode.ROOT_TOO_LONG: "Data store root is too long.",
        DataStoreDefinitionErrorCode.ENDPOINT_NOT_ALLOWED: (
            "Data store endpoint is not allowed."
        ),
        DataStoreDefinitionErrorCode.CREDENTIAL_REF_NOT_ALLOWED: (
            "Credential reference is not allowed for this data store kind."
        ),
        DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF: (
            "Data store credential reference is invalid."
        ),
        DataStoreDefinitionErrorCode.CREDENTIAL_REF_TOO_LONG: (
            "Data store credential reference is too long."
        ),
    }

    assert set(expected_messages) == set(DataStoreDefinitionErrorCode)
    for code, message in expected_messages.items():
        assert str(DataStoreDefinitionError(code)) == message


def test_storage_limits_match_the_persisted_columns() -> None:
    columns = DataStoreModel.__table__.c

    assert columns.name.type.length == DATA_STORE_NAME_MAX_LENGTH
    assert columns.root.type.length == DATA_STORE_ROOT_MAX_LENGTH
    assert columns.endpoint.type.length == DATA_STORE_ENDPOINT_MAX_LENGTH
    assert columns.credential_ref.type.length == DATA_STORE_CREDENTIAL_REF_MAX_LENGTH


@pytest.mark.parametrize("kind", sorted(RCLONE_BACKED_STORE_KINDS, key=lambda item: item.value))
def test_all_rclone_backed_store_kinds_are_supported(kind: StoreKind) -> None:
    definition = ValidatedDataStoreDefinition.create(
        name="analysis results",
        kind=kind,
        root="/project/data",
        credential_ref="shared remote",
    )

    assert definition.name == "analysis results"
    assert definition.kind is kind
    assert definition.root == "/project/data"
    assert definition.endpoint is None
    assert definition.credential_ref == "shared remote"


@pytest.mark.parametrize(
    ("root", "canonical"),
    [
        ("relative/path", "relative/path"),
        ("/rooted/path", "/rooted/path"),
        ("/", "/"),
    ],
)
def test_rclone_root_preserves_relative_and_rooted_semantics(
    root: str,
    canonical: str,
) -> None:
    definition = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.RCLONE,
        root=root,
    )

    assert definition.root == canonical
    assert definition.credential_ref is None


def test_http_and_git_roots_are_canonicalized_by_existing_parsers() -> None:
    http = ValidatedDataStoreDefinition.create(
        name="analysis results",
        kind=StoreKind.HTTP,
        root="HTTPS://EXAMPLE.COM:443/base",
    )
    git = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.GIT,
        root="SSH://User@EXAMPLE.COM:22/lab/repo.git",
    )

    assert http.root == "https://example.com/base/"
    assert http.name == "analysis results"
    assert git.root == "ssh://User@example.com/lab/repo.git"


def test_local_root_is_retained_exactly_without_requiring_it_to_exist(tmp_path: object) -> None:
    root = os.path.join(os.fspath(tmp_path), " path that need not exist ")
    definition = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.LOCAL_FS,
        root=root,
    )

    assert definition.root == root


def test_native_windows_drive_root_is_accepted_without_host_platform_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = "nt"
    windows_os.path = SimpleNamespace(**vars(ntpath))
    monkeypatch.setattr(local_path_policy, "os", windows_os)

    definition = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.LOCAL_FS,
        root=r"C:\Lab Tracker\Data",
    )

    assert definition.root == r"C:\Lab Tracker\Data"


@pytest.mark.parametrize(
    ("kind", "root"),
    [
        (StoreKind.HTTP, "https://example.com/data/"),
        (StoreKind.GIT, "https://example.com/lab/repo.git"),
        (StoreKind.RCLONE, "data"),
    ],
)
@pytest.mark.parametrize("name", [" analysis", "analysis ", "\tanalysis", "analysis\n"])
def test_surrounding_name_whitespace_is_rejected(
    kind: StoreKind,
    root: str,
    name: str,
) -> None:
    _assert_error(DataStoreDefinitionErrorCode.INVALID_NAME, name=name, kind=kind, root=root)


@pytest.mark.parametrize("kind", [StoreKind.OBJECT_TABLE, StoreKind.DATABASE])
def test_non_path_store_kinds_are_explicitly_unsupported(kind: StoreKind) -> None:
    _assert_error(DataStoreDefinitionErrorCode.UNSUPPORTED_KIND, kind=kind)


@pytest.mark.parametrize("kind", ["http", None, object(), 1])
def test_non_enum_store_kinds_are_rejected(kind: object) -> None:
    _assert_error(DataStoreDefinitionErrorCode.INVALID_KIND, kind=kind)


@pytest.mark.parametrize("name", [None, "", "bad/name", "bad#name", "bad\x00name"])
def test_generic_store_name_grammar_is_enforced(name: object) -> None:
    _assert_error(DataStoreDefinitionErrorCode.INVALID_NAME, name=name)


def test_name_length_limit_is_enforced_before_persistence() -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.NAME_TOO_LONG,
        name="n" * (DATA_STORE_NAME_MAX_LENGTH + 1),
    )
    _assert_error(
        DataStoreDefinitionErrorCode.NAME_TOO_LONG,
        name=f" {'n' * DATA_STORE_NAME_MAX_LENGTH} ",
    )


def test_exact_name_length_limit_is_accepted() -> None:
    name = "n" * DATA_STORE_NAME_MAX_LENGTH

    definition = ValidatedDataStoreDefinition.create(
        name=name,
        kind=StoreKind.HTTP,
        root="https://example.com/data/",
    )

    assert definition.name == name


@pytest.mark.parametrize("name", ["has space", "has:colon", "has@sign"])
def test_local_store_uses_the_stricter_name_grammar(
    tmp_path: object,
    name: str,
) -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.INVALID_NAME,
        name=name,
        kind=StoreKind.LOCAL_FS,
        root=os.fspath(tmp_path),
    )


@pytest.mark.parametrize(
    ("kind", "root", "credential_ref"),
    [
        (StoreKind.LOCAL_FS, os.path.abspath("data"), None),
        (StoreKind.HTTP, "https://example.com/data/", None),
        (StoreKind.GIT, "https://example.com/lab/repo.git", None),
        *[
            (kind, "data", "approved remote")
            for kind in sorted(
                RCLONE_BACKED_STORE_KINDS,
                key=lambda item: item.value,
            )
        ],
    ],
)
@pytest.mark.parametrize("endpoint", ["", " ", "https://secret.example/", object()])
def test_endpoint_is_forbidden_for_every_supported_store(
    kind: StoreKind,
    root: str,
    credential_ref: str | None,
    endpoint: object,
) -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.ENDPOINT_NOT_ALLOWED,
        kind=kind,
        root=root,
        credential_ref=credential_ref,
        endpoint=endpoint,
    )


@pytest.mark.parametrize(
    ("kind", "root"),
    [
        (StoreKind.LOCAL_FS, os.path.abspath("data")),
        (StoreKind.HTTP, "https://example.com/data/"),
        (StoreKind.GIT, "https://example.com/lab/repo.git"),
    ],
)
@pytest.mark.parametrize("credential_ref", ["", "secret remote", object()])
def test_credentials_are_forbidden_for_non_rclone_stores(
    kind: StoreKind,
    root: str,
    credential_ref: object,
) -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.CREDENTIAL_REF_NOT_ALLOWED,
        kind=kind,
        root=root,
        credential_ref=credential_ref,
    )


@pytest.mark.parametrize("credential_ref", ["", " remote", "remote ", "bad:remote", object()])
def test_rclone_credential_reference_uses_remote_name_grammar(
    credential_ref: object,
) -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF,
        kind=StoreKind.RCLONE,
        root="data",
        credential_ref=credential_ref,
    )


def test_rclone_credential_reference_length_is_bounded() -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.CREDENTIAL_REF_TOO_LONG,
        kind=StoreKind.RCLONE,
        root="data",
        credential_ref="r" * (DATA_STORE_CREDENTIAL_REF_MAX_LENGTH + 1),
    )


def test_exact_credential_reference_length_limit_is_accepted() -> None:
    credential_ref = "r" * DATA_STORE_CREDENTIAL_REF_MAX_LENGTH

    definition = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=StoreKind.RCLONE,
        root="data",
        credential_ref=credential_ref,
    )

    assert definition.credential_ref == credential_ref


@pytest.mark.parametrize(
    ("kind", "root"),
    [
        (StoreKind.LOCAL_FS, "relative/path"),
        (StoreKind.LOCAL_FS, f"{os.path.abspath('data')}\x00secret"),
        (StoreKind.LOCAL_FS, f"{os.path.abspath('data')}\x85secret"),
        (StoreKind.LOCAL_FS, f"{os.path.abspath('data')}\udcffsecret"),
        (StoreKind.HTTP, "https://user:password@example.com/data/"),
        (StoreKind.HTTP, "https://example.com/data/?query=secret"),
        (StoreKind.HTTP, "https://example.com/data/#fragment"),
        (StoreKind.HTTP, "https://example.com/data/\x00secret"),
        (StoreKind.RCLONE, "remote:path"),
        (StoreKind.RCLONE, "../escape"),
        (StoreKind.RCLONE, "data/\x00secret"),
        (StoreKind.RCLONE, ""),
        (StoreKind.GIT, "/local/repository"),
        (StoreKind.GIT, "https://user:password@example.com/repo.git"),
        (StoreKind.GIT, "https://example.com/repo.git?query=secret"),
        (StoreKind.GIT, "https://example.com/repo.git\x00secret"),
    ],
)
def test_backend_specific_root_grammar_is_enforced(
    kind: StoreKind,
    root: object,
) -> None:
    _assert_error(DataStoreDefinitionErrorCode.INVALID_ROOT, kind=kind, root=root)


def test_raw_and_canonical_root_lengths_are_bounded() -> None:
    _assert_error(
        DataStoreDefinitionErrorCode.ROOT_TOO_LONG,
        root="h" * (DATA_STORE_ROOT_MAX_LENGTH + 1),
    )
    raw_root = _http_root_with_length(DATA_STORE_ROOT_MAX_LENGTH)
    assert not raw_root.endswith("/")
    _assert_error(DataStoreDefinitionErrorCode.ROOT_TOO_LONG, root=raw_root)


@pytest.mark.parametrize(
    ("kind", "root"),
    [
        (
            StoreKind.HTTP,
            f"{_http_root_with_length(DATA_STORE_ROOT_MAX_LENGTH - 1)}/",
        ),
        (
            StoreKind.GIT,
            _http_root_with_length(DATA_STORE_ROOT_MAX_LENGTH),
        ),
        (
            StoreKind.RCLONE,
            _portable_path_with_length(DATA_STORE_ROOT_MAX_LENGTH, rooted=True),
        ),
    ],
)
def test_exact_root_length_limit_is_accepted(kind: StoreKind, root: str) -> None:
    definition = ValidatedDataStoreDefinition.create(
        name="analysis",
        kind=kind,
        root=root,
    )

    assert len(definition.root) == DATA_STORE_ROOT_MAX_LENGTH


@pytest.mark.parametrize(
    "overrides",
    [
        {"root": "https://user:top-secret@example.com/data/"},
        {"endpoint": "top-secret"},
        {
            "kind": StoreKind.RCLONE,
            "root": "data",
            "credential_ref": "top-secret:remote",
        },
    ],
)
def test_errors_do_not_echo_request_values(overrides: dict[str, object]) -> None:
    error = _assert_error(
        (
            DataStoreDefinitionErrorCode.ENDPOINT_NOT_ALLOWED
            if "endpoint" in overrides
            else DataStoreDefinitionErrorCode.INVALID_CREDENTIAL_REF
            if "credential_ref" in overrides
            else DataStoreDefinitionErrorCode.INVALID_ROOT
        ),
        **overrides,
    )

    assert "top-secret" not in str(error)
    assert "top-secret" not in repr(error)


def test_validation_performs_no_filesystem_network_or_process_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    def fail_io(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("data-store definition validation attempted I/O")

    isolated_path = SimpleNamespace(isabs=os.path.isabs)
    isolated_os = SimpleNamespace(
        fspath=os.fspath,
        name=os.name,
        path=isolated_path,
    )
    monkeypatch.setattr(local_path_policy, "os", isolated_os)
    # RegisteredHttpPrefix parsing is lexical. Replacing only its owning
    # module's socket binding proves validation never reaches DNS or transport
    # without poisoning pytest's process-global socket module.
    monkeypatch.setattr(outbound_http, "socket", SimpleNamespace())
    monkeypatch.setattr(builtins, "open", fail_io)
    monkeypatch.setattr(socket, "create_connection", fail_io)
    monkeypatch.setattr(socket, "getaddrinfo", fail_io)
    monkeypatch.setattr(subprocess, "Popen", fail_io)
    monkeypatch.setattr(subprocess, "run", fail_io)

    ValidatedDataStoreDefinition.create(
        name="local",
        kind=StoreKind.LOCAL_FS,
        root=os.fspath(tmp_path),
    )
    ValidatedDataStoreDefinition.create(
        name="http store",
        kind=StoreKind.HTTP,
        root="https://example.com/data",
    )
    ValidatedDataStoreDefinition.create(
        name="rclone store",
        kind=StoreKind.RCLONE,
        root="/data",
        credential_ref="remote",
    )
    ValidatedDataStoreDefinition.create(
        name="git store",
        kind=StoreKind.GIT,
        root="git@example.com:lab/repo.git",
    )
