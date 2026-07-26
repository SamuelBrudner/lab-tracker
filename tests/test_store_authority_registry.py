from __future__ import annotations

import builtins
import json
import os
import socket
import subprocess
import traceback
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

import lab_tracker.local_filesystem_authority as local_authority_module
from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.local_filesystem_authority import LocalFilesystemAuthorityBoundary
from lab_tracker.models import StoreCapability, StoreKind
from lab_tracker.store_authority_registry import (
    MAX_STORE_AUTHORITY_CONFIG_BYTES,
    MAX_STORE_AUTHORITY_CONFIG_CHARACTERS,
    MAX_STORE_AUTHORITY_GRANT_ID_LENGTH,
    MAX_STORE_AUTHORITY_GRANTS,
    MAX_STORE_AUTHORITY_JSON_DEPTH,
    STORE_AUTHORITY_CONFIG_SCHEMA,
    GitStoreAuthorityBoundary,
    GroupStoreScope,
    HttpStoreAuthorityBoundary,
    ProjectStoreScope,
    RcloneStoreAuthorityBoundary,
    StoreAuthorityProof,
    StoreAuthorityRegistry,
    StoreAuthorityRegistryError,
    StoreAuthorityRegistryErrorCode,
)

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_PROJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
GROUP_ID = UUID("33333333-3333-4333-8333-333333333333")
OTHER_GROUP_ID = UUID("44444444-4444-4444-8444-444444444444")
LOCAL_ROOT = r"C:\srv\lab" if os.name == "nt" else "/srv/lab"
LOCAL_CHILD = rf"{LOCAL_ROOT}\data" if os.name == "nt" else f"{LOCAL_ROOT}/data"


def _scope(*, project_id: UUID = PROJECT_ID) -> dict[str, str]:
    return {"project_id": str(project_id)}


def _group_scope(*, group_id: UUID = GROUP_ID) -> dict[str, str]:
    return {"group_id": str(group_id)}


def _grant(
    *,
    grant_id: str = "grant-1",
    scope: dict[str, str] | None = None,
    kind: StoreKind = StoreKind.LOCAL_FS,
    root: str = LOCAL_ROOT,
    capabilities: list[str] | None = None,
    remote: str | None = None,
    credential_mode: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "grant_id": grant_id,
        "scope": _scope() if scope is None else scope,
        "kind": kind.value,
        "root": root,
        "capabilities": (
            [StoreCapability.BYTES_BY_PATH.value] if capabilities is None else capabilities
        ),
    }
    if remote is not None:
        result["remote"] = remote
    if credential_mode is not None:
        result["credential_mode"] = credential_mode
    return result


def _rclone_grant(
    *,
    grant_id: str = "rclone-1",
    scope: dict[str, str] | None = None,
    kind: StoreKind = StoreKind.S3,
    root: str = "/approved",
    capabilities: list[str] | None = None,
    remote: str = "approved-remote",
    credential_mode: str = "credential_ref",
) -> dict[str, object]:
    return _grant(
        grant_id=grant_id,
        scope=scope,
        kind=kind,
        root=root,
        capabilities=capabilities,
        remote=remote,
        credential_mode=credential_mode,
    )


def _raw(*grants: dict[str, object]) -> str:
    return json.dumps(
        {"schema": STORE_AUTHORITY_CONFIG_SCHEMA, "grants": list(grants)},
        separators=(",", ":"),
    )


def _definition(
    *,
    name: str = "store",
    kind: StoreKind = StoreKind.LOCAL_FS,
    root: str = LOCAL_CHILD,
    credential_ref: str | None = None,
) -> ValidatedDataStoreDefinition:
    return ValidatedDataStoreDefinition.create(
        name=name,
        kind=kind,
        root=root,
        credential_ref=credential_ref,
    )


def _authorize(
    registry: StoreAuthorityRegistry,
    *,
    grant_id: str = "grant-1",
    scope: ProjectStoreScope | GroupStoreScope | None = None,
    candidate: ValidatedDataStoreDefinition | None = None,
    capabilities: object = (StoreCapability.BYTES_BY_PATH,),
) -> StoreAuthorityProof | None:
    return registry.authorize(
        grant_id=grant_id,
        scope=ProjectStoreScope(PROJECT_ID) if scope is None else scope,
        candidate=_definition() if candidate is None else candidate,
        capabilities=capabilities,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("raw", (None, ""))
def test_unset_and_exactly_empty_configuration_are_frozen_deny_all(raw: object) -> None:
    registry = StoreAuthorityRegistry.from_json(raw)

    assert registry.grant_count == 0
    assert not hasattr(registry, "__dict__")
    assert _authorize(registry) is None
    with pytest.raises(FrozenInstanceError):
        registry._grants = ()  # type: ignore[misc]


def test_valid_empty_versioned_envelope_is_deny_all() -> None:
    registry = StoreAuthorityRegistry.from_json(_raw())

    assert registry.grant_count == 0
    assert _authorize(registry) is None


def test_non_string_with_hostile_equality_is_rejected_without_executing_it() -> None:
    class HostileEquality:
        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("untrusted equality executed")

    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(HostileEquality())

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_TYPE


def test_explicit_matching_authorization_returns_the_same_sealed_proof() -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_grant()))

    first = _authorize(registry)
    second = _authorize(registry)

    assert first is not None
    assert second is first
    assert first.grant_id == "grant-1"
    assert first.fingerprint.startswith("sag-v1-sha256:")
    assert len(first.fingerprint) == len("sag-v1-sha256:") + 64
    assert not hasattr(first, "__dict__")
    assert "srv" not in repr(first)
    with pytest.raises(FrozenInstanceError):
        first.fingerprint = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="must be built by the registry"):
        StoreAuthorityProof(
            grant_id="forged",
            fingerprint="sag-v1-sha256:" + "0" * 64,
            _factory_token=object(),
        )


def test_registry_and_typed_boundaries_cannot_be_forged_through_public_initializers() -> None:
    with pytest.raises(TypeError, match="must be built by its factory"):
        StoreAuthorityRegistry((), _factory_token=object())
    with pytest.raises(TypeError, match="must be built by its parser"):
        LocalFilesystemAuthorityBoundary(
            flavor="posix",
            rendered="/secret",
            anchor="/",
            components=("secret",),
            _factory_token=object(),
        )
    with pytest.raises(TypeError, match="registry-owned"):
        HttpStoreAuthorityBoundary(
            origin="https://example.test",
            components=("secret",),
            _factory_token=object(),
        )
    with pytest.raises(TypeError, match="registry-owned"):
        RcloneStoreAuthorityBoundary(
            remote="secret",
            rooted=True,
            components=("secret",),
            _factory_token=object(),
        )
    with pytest.raises(TypeError, match="registry-owned"):
        GitStoreAuthorityBoundary(
            scheme="ssh",
            host="example.test",
            effective_port=22,
            ssh_user="git",
            path_style="url",
            host_is_ipv6=False,
            components=("secret",),
            _factory_token=object(),
        )


@pytest.mark.parametrize(
    ("raw", "code"),
    (
        (0, StoreAuthorityRegistryErrorCode.INVALID_TYPE),
        (False, StoreAuthorityRegistryErrorCode.INVALID_TYPE),
        (b"{}", StoreAuthorityRegistryErrorCode.INVALID_TYPE),
        (" ", StoreAuthorityRegistryErrorCode.INVALID_JSON),
        ("null", StoreAuthorityRegistryErrorCode.INVALID_SCHEMA),
        ("{}", StoreAuthorityRegistryErrorCode.INVALID_SCHEMA),
        (
            '{"schema":"wrong","grants":[]}',
            StoreAuthorityRegistryErrorCode.INVALID_SCHEMA,
        ),
        (
            '{"schema":"lab-tracker/store-authority/v1","grants":NaN}',
            StoreAuthorityRegistryErrorCode.INVALID_JSON,
        ),
        (
            '{"schema":"lab-tracker/store-authority/v1","grants":Infinity}',
            StoreAuthorityRegistryErrorCode.INVALID_JSON,
        ),
        (
            '{"schema":"lab-tracker/store-authority/v1","grants":[],"grants":[]}',
            StoreAuthorityRegistryErrorCode.INVALID_JSON,
        ),
        (
            '{"schema":"lab-tracker/store-authority/v1","grants":[]}\n',
            StoreAuthorityRegistryErrorCode.INVALID_TEXT,
        ),
        (
            '{"schema":"lab-tracker/store-authority/v1","grants":[]}\ud800',
            StoreAuthorityRegistryErrorCode.INVALID_TEXT,
        ),
    ),
)
def test_strict_parser_reports_static_error_codes(
    raw: object,
    code: StoreAuthorityRegistryErrorCode,
) -> None:
    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(raw)

    assert captured.value.code is code


def test_error_messages_cover_all_codes_and_are_static() -> None:
    expected = {
        StoreAuthorityRegistryErrorCode.INVALID_TYPE: (
            "Store authority grant configuration must be a string."
        ),
        StoreAuthorityRegistryErrorCode.TOO_LARGE: (
            "Store authority grant configuration exceeds its safe limit."
        ),
        StoreAuthorityRegistryErrorCode.INVALID_TEXT: (
            "Store authority grant configuration contains invalid text."
        ),
        StoreAuthorityRegistryErrorCode.INVALID_JSON: (
            "Store authority grant configuration is not valid strict JSON."
        ),
        StoreAuthorityRegistryErrorCode.INVALID_SCHEMA: (
            "Store authority grant configuration has an invalid schema."
        ),
        StoreAuthorityRegistryErrorCode.INVALID_GRANT: (
            "Store authority grant configuration contains an invalid grant."
        ),
        StoreAuthorityRegistryErrorCode.DUPLICATE_GRANT_ID: (
            "Store authority grant configuration contains a duplicate grant identifier."
        ),
        StoreAuthorityRegistryErrorCode.AMBIGUOUS_GRANTS: (
            "Store authority grant configuration contains ambiguous grants."
        ),
    }

    assert set(expected) == set(StoreAuthorityRegistryErrorCode)
    for code, message in expected.items():
        error = StoreAuthorityRegistryError(code)
        assert str(error) == message
        assert "secret" not in repr(error)


def test_size_limits_apply_independently_to_characters_and_utf8_bytes() -> None:
    with pytest.raises(StoreAuthorityRegistryError) as characters:
        StoreAuthorityRegistry.from_json("x" * (MAX_STORE_AUTHORITY_CONFIG_CHARACTERS + 1))
    with pytest.raises(StoreAuthorityRegistryError) as encoded:
        StoreAuthorityRegistry.from_json("é" * (MAX_STORE_AUTHORITY_CONFIG_BYTES // 2 + 1))

    assert characters.value.code is StoreAuthorityRegistryErrorCode.TOO_LARGE
    assert encoded.value.code is StoreAuthorityRegistryErrorCode.TOO_LARGE


def test_exact_character_and_byte_limit_is_accepted() -> None:
    envelope = _raw()
    raw = envelope + " " * (MAX_STORE_AUTHORITY_CONFIG_CHARACTERS - len(envelope))

    assert len(raw) == MAX_STORE_AUTHORITY_CONFIG_CHARACTERS
    assert len(raw.encode("utf-8")) == MAX_STORE_AUTHORITY_CONFIG_BYTES
    assert StoreAuthorityRegistry.from_json(raw).grant_count == 0


def test_excessive_depth_count_and_object_width_fail_before_grant_parsing() -> None:
    nested: object = []
    for _ in range(MAX_STORE_AUTHORITY_JSON_DEPTH + 1):
        nested = [nested]
    too_deep = json.dumps(
        {"schema": STORE_AUTHORITY_CONFIG_SCHEMA, "grants": nested},
        separators=(",", ":"),
    )
    too_many = _raw(
        *(
            _grant(
                grant_id=f"g-{index}",
                scope=_scope(project_id=UUID(int=index + 1)),
                root=(rf"C:\srv\grant-{index}" if os.name == "nt" else f"/srv/grant-{index}"),
            )
            for index in range(MAX_STORE_AUTHORITY_GRANTS + 1)
        )
    )
    too_wide = json.dumps(
        {
            "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
            "grants": [],
            **{f"extra-{index}": index for index in range(9)},
        },
        separators=(",", ":"),
    )

    for raw in (too_deep, too_many, too_wide):
        with pytest.raises(StoreAuthorityRegistryError) as captured:
            StoreAuthorityRegistry.from_json(raw)
        assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_SCHEMA


def test_exact_maximum_grant_count_is_accepted() -> None:
    grants = tuple(
        _grant(
            grant_id=f"g-{index}",
            scope=_scope(project_id=UUID(int=index + 1)),
            root=(rf"C:\srv\grant-{index}" if os.name == "nt" else f"/srv/grant-{index}"),
        )
        for index in range(MAX_STORE_AUTHORITY_GRANTS)
    )

    assert StoreAuthorityRegistry.from_json(_raw(*grants)).grant_count == (
        MAX_STORE_AUTHORITY_GRANTS
    )


def test_rejected_secret_is_absent_from_exception_chain_traceback_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "operator-password-SHOULD-NOT-LEAK"
    raw = _raw({**_grant(), "password": secret})

    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(raw)

    error = captured.value
    rendered = "".join(traceback.format_exception(error))
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in rendered
    assert secret not in caplog.text


@pytest.mark.parametrize(
    "grant_id",
    (
        "",
        "a" * (MAX_STORE_AUTHORITY_GRANT_ID_LENGTH + 1),
        "-leading",
        "_leading",
        ".leading",
        "has space",
        "has/slash",
        "has:colon",
        "unicodé",
        "line\nbreak",
    ),
)
def test_grant_id_uses_the_exact_conservative_ascii_grammar(grant_id: str) -> None:
    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(_raw(_grant(grant_id=grant_id)))

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


@pytest.mark.parametrize(
    "grant_id",
    (
        "a",
        "A0",
        "grant.with_dots-and-hyphens",
        "a" + "-" * (MAX_STORE_AUTHORITY_GRANT_ID_LENGTH - 1),
    ),
)
def test_valid_grant_id_boundaries_are_accepted(grant_id: str) -> None:
    assert StoreAuthorityRegistry.from_json(_raw(_grant(grant_id=grant_id))).grant_count == 1


@pytest.mark.parametrize(
    "scope",
    (
        {},
        {"project_id": str(PROJECT_ID), "group_id": str(GROUP_ID)},
        {"project_id": str(PROJECT_ID), "extra": "value"},
        {"id": str(PROJECT_ID)},
        {"project_id": str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")).upper()},
        {"project_id": PROJECT_ID.hex},
        {"project_id": f"{{{PROJECT_ID}}}"},
        {"project_id": 1},
        {"group_id": str(UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")).upper()},
        {"group_id": None},
        ["project_id", str(PROJECT_ID)],
    ),
)
def test_scope_requires_exact_xor_field_and_canonical_uuid(scope: object) -> None:
    grant = _grant()
    grant["scope"] = scope

    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(_raw(grant))

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


def test_project_and_group_scopes_with_the_same_uuid_remain_distinct() -> None:
    shared = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(grant_id="project", scope=_scope(project_id=shared)),
            _grant(grant_id="group", scope=_group_scope(group_id=shared)),
        )
    )
    candidate = _definition()

    assert (
        _authorize(
            registry,
            grant_id="project",
            scope=ProjectStoreScope(shared),
            candidate=candidate,
        )
        is not None
    )
    assert (
        _authorize(
            registry,
            grant_id="project",
            scope=GroupStoreScope(shared),
            candidate=candidate,
        )
        is None
    )
    assert (
        _authorize(
            registry,
            grant_id="group",
            scope=GroupStoreScope(shared),
            candidate=candidate,
        )
        is not None
    )


@pytest.mark.parametrize(
    "kind",
    (
        StoreKind.OBJECT_TABLE.value,
        StoreKind.DATABASE.value,
        "unknown",
        "",
        None,
        1,
    ),
)
def test_unknown_and_not_yet_supported_store_kinds_fail_closed(kind: object) -> None:
    grant = _grant()
    grant["kind"] = kind

    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(_raw(grant))

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


@pytest.mark.parametrize(
    "extra",
    (
        {"endpoint": "https://secret.invalid"},
        {"password": "secret"},
        {"token": "secret"},
        {"connection_string": "postgresql://secret"},
        {"remote": "unexpected"},
        {"credential_mode": "credential_ref"},
    ),
)
def test_non_rclone_grants_reject_every_extra_or_secret_field(
    extra: dict[str, object],
) -> None:
    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(_raw({**_grant(), **extra}))

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


@pytest.mark.parametrize(
    ("mutation",),
    (
        ({"remote": None},),
        ({"credential_mode": None},),
        ({"credential_mode": "unknown"},),
        ({"endpoint": "https://secret.invalid"},),
        ({"password": "secret"},),
    ),
)
def test_rclone_grants_require_exact_additional_fields(
    mutation: dict[str, object],
) -> None:
    grant = _rclone_grant()
    grant.update(mutation)

    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(_raw(grant))

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


def test_rclone_grants_reject_missing_remote_or_credential_mode() -> None:
    for missing in ("remote", "credential_mode"):
        grant = _rclone_grant()
        del grant[missing]
        with pytest.raises(StoreAuthorityRegistryError) as captured:
            StoreAuthorityRegistry.from_json(_raw(grant))
        assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


@pytest.mark.parametrize(
    ("kind", "supported"),
    (
        (
            StoreKind.LOCAL_FS,
            frozenset(
                {
                    StoreCapability.BYTES_BY_PATH,
                    StoreCapability.BYTE_RANGE,
                    StoreCapability.LIST,
                }
            ),
        ),
        (
            StoreKind.HTTP,
            frozenset(
                {
                    StoreCapability.BYTES_BY_PATH,
                    StoreCapability.BYTE_RANGE,
                }
            ),
        ),
        (
            StoreKind.GIT,
            frozenset(
                {
                    StoreCapability.BYTES_BY_PATH,
                    StoreCapability.BYTE_RANGE,
                    StoreCapability.VERSIONED_SNAPSHOT,
                }
            ),
        ),
        (
            StoreKind.S3,
            frozenset(
                {
                    StoreCapability.BYTES_BY_PATH,
                    StoreCapability.BYTE_RANGE,
                    StoreCapability.LIST,
                    StoreCapability.VERSIONED_SNAPSHOT,
                }
            ),
        ),
        (
            StoreKind.GDRIVE,
            frozenset(
                {
                    StoreCapability.BYTES_BY_PATH,
                    StoreCapability.BYTE_RANGE,
                    StoreCapability.LIST,
                }
            ),
        ),
    ),
)
def test_capabilities_are_nonempty_duplicate_free_typed_kind_subsets(
    kind: StoreKind,
    supported: frozenset[StoreCapability],
) -> None:
    if kind is StoreKind.LOCAL_FS:
        grant = _grant(
            kind=kind,
            root=LOCAL_ROOT,
            capabilities=[capability.value for capability in supported],
        )
    elif kind is StoreKind.HTTP:
        grant = _grant(
            kind=kind,
            root="https://example.test/base/",
            capabilities=[capability.value for capability in supported],
        )
    elif kind is StoreKind.GIT:
        grant = _grant(
            kind=kind,
            root="ssh://git@example.test/org/repository",
            capabilities=[capability.value for capability in supported],
        )
    else:
        grant = _rclone_grant(
            kind=kind,
            capabilities=[capability.value for capability in supported],
        )

    assert StoreAuthorityRegistry.from_json(_raw(grant)).grant_count == 1
    for invalid in (
        [],
        [StoreCapability.BYTES_BY_PATH.value] * 2,
        ["unknown"],
        [StoreCapability.QUERY.value],
        [StoreCapability.BYTES_BY_PATH.value, 1],
        StoreCapability.BYTES_BY_PATH.value,
    ):
        rejected = {**grant, "capabilities": invalid}
        with pytest.raises(StoreAuthorityRegistryError) as captured:
            StoreAuthorityRegistry.from_json(_raw(rejected))
        assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


def test_duplicate_grant_ids_fail_even_when_scopes_and_boundaries_differ() -> None:
    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(
            _raw(
                _grant(grant_id="same"),
                _grant(
                    grant_id="same",
                    scope=_group_scope(),
                    root=(r"C:\different" if os.name == "nt" else "/different"),
                ),
            )
        )

    assert captured.value.code is StoreAuthorityRegistryErrorCode.DUPLICATE_GRANT_ID


def test_duplicate_keys_are_rejected_at_nested_object_depths() -> None:
    duplicate_grant_key = (
        '{"schema":"lab-tracker/store-authority/v1","grants":[{'
        '"grant_id":"a","grant_id":"b",'
        f'"scope":{{"project_id":"{PROJECT_ID}"}},'
        f'"kind":"local_fs","root":{json.dumps(LOCAL_ROOT)},'
        '"capabilities":["bytes_by_path"]}]}'
    )
    duplicate_scope_key = (
        '{"schema":"lab-tracker/store-authority/v1","grants":[{'
        '"grant_id":"a",'
        f'"scope":{{"project_id":"{PROJECT_ID}","project_id":"{OTHER_PROJECT_ID}"}},'
        f'"kind":"local_fs","root":{json.dumps(LOCAL_ROOT)},'
        '"capabilities":["bytes_by_path"]}]}'
    )

    for raw in (duplicate_grant_key, duplicate_scope_key):
        with pytest.raises(StoreAuthorityRegistryError) as captured:
            StoreAuthorityRegistry.from_json(raw)
        assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_JSON


@pytest.mark.parametrize("escaped", ("\\u0000", "\\u0085", "\\ud800"))
def test_escaped_controls_and_surrogates_in_grant_values_fail_safely(
    escaped: str,
) -> None:
    raw = (
        '{"schema":"lab-tracker/store-authority/v1","grants":['
        '{"grant_id":"g","scope":{"project_id":"'
        f"{PROJECT_ID}"
        '"},"kind":"local_fs","root":"'
        f"{escaped}"
        '","capabilities":["bytes_by_path"]}]}'
    )

    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(raw)

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def _local_path(*components: str, drive: str = "C") -> str:
    if os.name == "nt":
        return f"{drive}:\\" + "\\".join(components)
    return "/" + "/".join(components)


@pytest.mark.parametrize(
    ("candidate_root", "allowed"),
    (
        (_local_path("srv", "lab"), True),
        (_local_path("srv", "lab", "nested", "store"), True),
        (_local_path("srv"), False),
        (_local_path("srv", "laboratory"), False),
        (_local_path("srv", "other"), False),
        (_local_path("srv", "Lab", "nested"), False),
    ),
)
def test_local_authority_uses_exact_component_containment(
    candidate_root: str,
    allowed: bool,
) -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_grant(root=_local_path("srv", "lab"))))
    candidate = _definition(root=candidate_root)

    assert (_authorize(registry, candidate=candidate) is not None) is allowed


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows definitions")
def test_windows_local_authority_normalizes_only_drive_and_separator_aliases() -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_grant(root=r"c:/Approved//Lab\\")))

    canonical = _definition(root=r"C:\Approved\Lab\store")
    case_alias = _definition(root=r"c:\approved\Lab\store")
    other_drive = _definition(root=r"D:\Approved\Lab\store")

    assert _authorize(registry, candidate=canonical) is not None
    assert _authorize(registry, candidate=case_alias) is None
    assert _authorize(registry, candidate=other_drive) is None


@pytest.mark.skipif(os.name != "posix", reason="requires native POSIX definitions")
@pytest.mark.parametrize(
    "root",
    (
        "relative/path",
        "//ambiguous/namespace",
        "/srv//ambiguous",
        "/srv/./ambiguous",
        "/srv/../ambiguous",
        "/srv/control\x85",
        "/srv/\ud800",
    ),
)
def test_posix_local_grants_reject_non_strict_lexical_boundaries(root: str) -> None:
    with pytest.raises(StoreAuthorityRegistryError) as captured:
        StoreAuthorityRegistry.from_json(_raw(_grant(root=root)))

    assert captured.value.code is StoreAuthorityRegistryErrorCode.INVALID_GRANT


@pytest.mark.parametrize(
    ("candidate_root", "allowed"),
    (
        ("https://example.test/base/", True),
        ("https://EXAMPLE.test:443/base/nested/", True),
        ("https://example.test/", False),
        ("https://example.test/baseball/", False),
        ("https://example.test/other/", False),
        ("https://example.test:8443/base/nested/", False),
        ("http://example.test/base/nested/", False),
        ("https://other.test/base/nested/", False),
    ),
)
def test_http_authority_preserves_origin_and_ordered_prefix_components(
    candidate_root: str,
    allowed: bool,
) -> None:
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(
                kind=StoreKind.HTTP,
                root="https://example.test:443/base/",
                capabilities=[StoreCapability.BYTES_BY_PATH.value],
            )
        )
    )
    candidate = _definition(kind=StoreKind.HTTP, root=candidate_root)

    assert (_authorize(registry, candidate=candidate) is not None) is allowed


@pytest.mark.parametrize(
    ("mode", "candidate_name", "credential_ref", "allowed"),
    (
        ("name_fallback", "approved-remote", None, True),
        ("name_fallback", "other-remote", None, False),
        ("name_fallback", "store", "approved-remote", False),
        ("credential_ref", "store", "approved-remote", True),
        ("credential_ref", "approved-remote", None, False),
        ("credential_ref", "store", "other-remote", False),
    ),
)
def test_rclone_authority_requires_exact_remote_and_credential_mode(
    mode: str,
    candidate_name: str,
    credential_ref: str | None,
    allowed: bool,
) -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_rclone_grant(credential_mode=mode)))
    candidate = _definition(
        name=candidate_name,
        kind=StoreKind.S3,
        root="/approved/nested",
        credential_ref=credential_ref,
    )

    assert (_authorize(registry, grant_id="rclone-1", candidate=candidate) is not None) is allowed


@pytest.mark.parametrize(
    ("candidate_root", "allowed"),
    (
        ("/approved", True),
        ("/approved/nested/path", True),
        ("/", False),
        ("/approve", False),
        ("/other", False),
        ("approved/nested", False),
    ),
)
def test_rclone_authority_preserves_rootedness_and_ordered_components(
    candidate_root: str,
    allowed: bool,
) -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_rclone_grant()))
    candidate = _definition(
        kind=StoreKind.S3,
        root=candidate_root,
        credential_ref="approved-remote",
    )

    assert (_authorize(registry, grant_id="rclone-1", candidate=candidate) is not None) is allowed


def test_relative_rclone_grant_never_authorizes_rooted_candidate() -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_rclone_grant(root="approved")))
    relative = _definition(
        kind=StoreKind.S3,
        root="approved/nested",
        credential_ref="approved-remote",
    )
    rooted = _definition(
        kind=StoreKind.S3,
        root="/approved/nested",
        credential_ref="approved-remote",
    )

    assert _authorize(registry, grant_id="rclone-1", candidate=relative) is not None
    assert _authorize(registry, grant_id="rclone-1", candidate=rooted) is None


@pytest.mark.parametrize(
    "kind",
    (
        StoreKind.SSH,
        StoreKind.S3,
        StoreKind.GCS,
        StoreKind.AZURE_BLOB,
        StoreKind.DROPBOX,
        StoreKind.GDRIVE,
        StoreKind.BOX,
        StoreKind.ONEDRIVE,
        StoreKind.RCLONE,
    ),
)
def test_every_rclone_backed_kind_uses_the_same_typed_authority_contract(
    kind: StoreKind,
) -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_rclone_grant(kind=kind)))
    candidate = _definition(
        kind=kind,
        root="/approved/child",
        credential_ref="approved-remote",
    )

    assert _authorize(registry, grant_id="rclone-1", candidate=candidate) is not None


@pytest.mark.parametrize(
    ("candidate_root", "allowed"),
    (
        ("ssh://git@example.test/org", True),
        ("ssh://git@example.test/org/repository", True),
        ("ssh://git@example.test/", False),
        ("ssh://git@example.test/organization/repository", False),
        ("ssh://other@example.test/org/repository", False),
        ("ssh://git@example.test:2222/org/repository", False),
        ("ssh://git@other.test/org/repository", False),
        ("git@example.test:org/repository", False),
        ("https://example.test/org/repository", False),
    ),
)
def test_git_authority_preserves_full_remote_family_and_ordered_components(
    candidate_root: str,
    allowed: bool,
) -> None:
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(
                kind=StoreKind.GIT,
                root="ssh://git@example.test/org",
                capabilities=[StoreCapability.BYTES_BY_PATH.value],
            )
        )
    )
    candidate = _definition(kind=StoreKind.GIT, root=candidate_root)

    assert (_authorize(registry, candidate=candidate) is not None) is allowed


def test_authorization_requires_exact_id_scope_kind_and_capability_subset() -> None:
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(
                capabilities=[
                    StoreCapability.BYTES_BY_PATH.value,
                    StoreCapability.BYTE_RANGE.value,
                ]
            )
        )
    )
    candidate = _definition()

    assert (
        _authorize(
            registry,
            candidate=candidate,
            capabilities=(StoreCapability.BYTES_BY_PATH,),
        )
        is not None
    )
    assert (
        _authorize(
            registry,
            candidate=candidate,
            capabilities=(
                StoreCapability.BYTES_BY_PATH,
                StoreCapability.BYTE_RANGE,
            ),
        )
        is not None
    )
    assert _authorize(registry, grant_id="missing", candidate=candidate) is None
    assert (
        _authorize(
            registry,
            scope=ProjectStoreScope(OTHER_PROJECT_ID),
            candidate=candidate,
        )
        is None
    )
    assert (
        _authorize(
            registry,
            scope=GroupStoreScope(GROUP_ID),
            candidate=candidate,
        )
        is None
    )
    assert (
        _authorize(
            registry,
            candidate=candidate,
            capabilities=(StoreCapability.LIST,),
        )
        is None
    )
    assert (
        _authorize(
            registry,
            candidate=_definition(
                kind=StoreKind.HTTP,
                root="https://example.test/base/",
            ),
        )
        is None
    )


@pytest.mark.parametrize(
    "capabilities",
    (
        (),
        (StoreCapability.BYTES_BY_PATH, StoreCapability.BYTES_BY_PATH),
        ("bytes_by_path",),
        (StoreCapability.BYTES_BY_PATH, "byte_range"),
        None,
        1,
    ),
)
def test_authorization_rejects_empty_duplicate_or_untyped_capabilities(
    capabilities: object,
) -> None:
    registry = StoreAuthorityRegistry.from_json(_raw(_grant()))

    assert _authorize(registry, capabilities=capabilities) is None


def test_authorization_consumes_only_a_bounded_prefix_of_an_infinite_iterable() -> None:
    observed = 0

    def infinite() -> Any:
        nonlocal observed
        while True:
            observed += 1
            yield StoreCapability.BYTES_BY_PATH

    registry = StoreAuthorityRegistry.from_json(_raw(_grant()))

    assert _authorize(registry, capabilities=infinite()) is None
    assert observed == len(StoreCapability) + 1


def test_authorization_treats_an_exploding_capability_iterable_as_a_mismatch() -> None:
    class ExplodingCapabilities:
        def __iter__(self) -> Any:
            raise RuntimeError("untrusted iterator detail")

    registry = StoreAuthorityRegistry.from_json(_raw(_grant()))

    assert _authorize(registry, capabilities=ExplodingCapabilities()) is None


@pytest.mark.parametrize(
    "grants",
    (
        (
            _grant(grant_id="parent", root=_local_path("srv", "lab")),
            _grant(grant_id="child", root=_local_path("srv", "lab", "child")),
        ),
        (
            _grant(
                grant_id="parent",
                kind=StoreKind.HTTP,
                root="https://example.test/base/",
            ),
            _grant(
                grant_id="child",
                kind=StoreKind.HTTP,
                root="https://example.test/base/child/",
            ),
        ),
        (
            _rclone_grant(
                grant_id="parent",
                root="/base",
                credential_mode="name_fallback",
            ),
            _rclone_grant(
                grant_id="child",
                root="/base/child",
                credential_mode="credential_ref",
            ),
        ),
        (
            _grant(
                grant_id="parent",
                kind=StoreKind.GIT,
                root="ssh://git@example.test/org",
            ),
            _grant(
                grant_id="child",
                kind=StoreKind.GIT,
                root="ssh://git@example.test/org/repository",
            ),
        ),
    ),
)
def test_same_scope_kind_and_family_rejects_ancestor_overlap_symmetrically(
    grants: tuple[dict[str, object], dict[str, object]],
) -> None:
    first, second = grants
    for ordered in ((first, second), (second, first)):
        with pytest.raises(StoreAuthorityRegistryError) as captured:
            StoreAuthorityRegistry.from_json(_raw(*ordered))
        assert captured.value.code is StoreAuthorityRegistryErrorCode.AMBIGUOUS_GRANTS


def test_semantically_equivalent_boundaries_are_ambiguous() -> None:
    if os.name == "nt":
        first_root = r"c:/Approved//Lab\\"
        second_root = r"C:\Approved\Lab"
    else:
        first_root = "/srv/lab/"
        second_root = "/srv/lab"
    pairs = (
        (
            _grant(grant_id="a", root=first_root),
            _grant(grant_id="b", root=second_root),
        ),
        (
            _grant(
                grant_id="a",
                kind=StoreKind.HTTP,
                root="https://EXAMPLE.test:443/base",
            ),
            _grant(
                grant_id="b",
                kind=StoreKind.HTTP,
                root="https://example.test/base/",
            ),
        ),
    )

    for first, second in pairs:
        with pytest.raises(StoreAuthorityRegistryError) as captured:
            StoreAuthorityRegistry.from_json(_raw(first, second))
        assert captured.value.code is StoreAuthorityRegistryErrorCode.AMBIGUOUS_GRANTS


def test_cross_scope_overlaps_are_intentionally_allowed() -> None:
    shared_root = _local_path("srv", "shared")
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(grant_id="project-a", root=shared_root),
            _grant(
                grant_id="project-b",
                scope=_scope(project_id=OTHER_PROJECT_ID),
                root=shared_root,
            ),
            _grant(
                grant_id="group",
                scope=_group_scope(),
                root=shared_root,
            ),
        )
    )

    assert registry.grant_count == 3
    candidate = _definition(root=shared_root)
    assert (
        _authorize(
            registry,
            grant_id="project-a",
            scope=ProjectStoreScope(PROJECT_ID),
            candidate=candidate,
        )
        is not None
    )
    assert (
        _authorize(
            registry,
            grant_id="project-b",
            scope=ProjectStoreScope(OTHER_PROJECT_ID),
            candidate=candidate,
        )
        is not None
    )
    assert (
        _authorize(
            registry,
            grant_id="group",
            scope=GroupStoreScope(GROUP_ID),
            candidate=candidate,
        )
        is not None
    )


def test_same_scope_nonoverlapping_families_siblings_and_kinds_are_allowed() -> None:
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(
                grant_id="http-a",
                kind=StoreKind.HTTP,
                root="https://a.example.test/base/",
            ),
            _grant(
                grant_id="http-b",
                kind=StoreKind.HTTP,
                root="https://b.example.test/base/",
            ),
            _grant(
                grant_id="http-sibling",
                kind=StoreKind.HTTP,
                root="https://a.example.test/other/",
            ),
            _rclone_grant(
                grant_id="s3",
                kind=StoreKind.S3,
                remote="same-remote",
            ),
            _rclone_grant(
                grant_id="gcs",
                kind=StoreKind.GCS,
                remote="same-remote",
            ),
            _rclone_grant(
                grant_id="s3-relative",
                kind=StoreKind.S3,
                root="approved",
                remote="same-remote",
            ),
        )
    )

    assert registry.grant_count == 6


def _proof_for_http(
    grant: dict[str, object],
    *,
    scope: ProjectStoreScope | GroupStoreScope | None = None,
) -> StoreAuthorityProof:
    registry = StoreAuthorityRegistry.from_json(_raw(grant))
    proof = _authorize(
        registry,
        grant_id=str(grant["grant_id"]),
        scope=ProjectStoreScope(PROJECT_ID) if scope is None else scope,
        candidate=_definition(
            kind=StoreKind.HTTP,
            root=str(grant["root"]),
        ),
        capabilities=tuple(
            StoreCapability(value) for value in cast(list[str], grant["capabilities"])
        ),
    )
    assert proof is not None
    return proof


def _proof_for_local(
    grant: dict[str, object],
    *,
    scope: ProjectStoreScope | GroupStoreScope | None = None,
) -> StoreAuthorityProof:
    registry = StoreAuthorityRegistry.from_json(_raw(grant))
    proof = _authorize(
        registry,
        grant_id=str(grant["grant_id"]),
        scope=ProjectStoreScope(PROJECT_ID) if scope is None else scope,
        candidate=_definition(root=str(grant["root"])),
        capabilities=tuple(
            StoreCapability(value) for value in cast(list[str], grant["capabilities"])
        ),
    )
    assert proof is not None
    return proof


def _proof_for_rclone(
    grant: dict[str, object],
    *,
    scope: ProjectStoreScope | GroupStoreScope | None = None,
) -> StoreAuthorityProof:
    mode = grant["credential_mode"]
    remote = str(grant["remote"])
    registry = StoreAuthorityRegistry.from_json(_raw(grant))
    proof = _authorize(
        registry,
        grant_id=str(grant["grant_id"]),
        scope=ProjectStoreScope(PROJECT_ID) if scope is None else scope,
        candidate=_definition(
            name=remote if mode == "name_fallback" else "store",
            kind=StoreKind(str(grant["kind"])),
            root=str(grant["root"]),
            credential_ref=remote if mode == "credential_ref" else None,
        ),
        capabilities=tuple(
            StoreCapability(value) for value in cast(list[str], grant["capabilities"])
        ),
    )
    assert proof is not None
    return proof


def _proof_for_git(
    grant: dict[str, object],
    *,
    scope: ProjectStoreScope | GroupStoreScope | None = None,
) -> StoreAuthorityProof:
    registry = StoreAuthorityRegistry.from_json(_raw(grant))
    proof = _authorize(
        registry,
        grant_id=str(grant["grant_id"]),
        scope=ProjectStoreScope(PROJECT_ID) if scope is None else scope,
        candidate=_definition(
            kind=StoreKind.GIT,
            root=str(grant["root"]),
        ),
        capabilities=tuple(
            StoreCapability(value) for value in cast(list[str], grant["capabilities"])
        ),
    )
    assert proof is not None
    return proof


def test_fingerprint_is_independent_of_raw_id_and_capability_input_order() -> None:
    first = _grant(
        grant_id="first",
        kind=StoreKind.HTTP,
        root="https://EXAMPLE.test:443/base",
        capabilities=[
            StoreCapability.BYTE_RANGE.value,
            StoreCapability.BYTES_BY_PATH.value,
        ],
    )
    second = _grant(
        grant_id="second",
        kind=StoreKind.HTTP,
        root="https://example.test/base/",
        capabilities=[
            StoreCapability.BYTES_BY_PATH.value,
            StoreCapability.BYTE_RANGE.value,
        ],
    )

    assert _proof_for_http(first).fingerprint == _proof_for_http(second).fingerprint


def test_fingerprint_type_tags_scope_and_preserves_ordered_path_components() -> None:
    project = _grant(
        kind=StoreKind.HTTP,
        root="https://example.test/alpha/beta/",
    )
    group = {**project, "scope": _group_scope()}
    reversed_path = {
        **project,
        "root": "https://example.test/beta/alpha/",
    }

    project_fingerprint = _proof_for_http(project).fingerprint
    group_fingerprint = _proof_for_http(
        group,
        scope=GroupStoreScope(GROUP_ID),
    ).fingerprint
    reversed_fingerprint = _proof_for_http(reversed_path).fingerprint

    assert project_fingerprint != group_fingerprint
    assert project_fingerprint != reversed_fingerprint


def test_fingerprint_includes_exact_scope_uuid() -> None:
    baseline = _grant(
        kind=StoreKind.HTTP,
        root="https://example.test/alpha/beta/",
    )
    other_scope = {
        **baseline,
        "scope": _scope(project_id=OTHER_PROJECT_ID),
    }

    assert _proof_for_http(baseline).fingerprint != _proof_for_http(
        other_scope,
        scope=ProjectStoreScope(OTHER_PROJECT_ID),
    ).fingerprint


def test_fingerprint_includes_store_kind() -> None:
    s3 = _rclone_grant(kind=StoreKind.S3)
    gcs = _rclone_grant(kind=StoreKind.GCS)

    assert _proof_for_rclone(s3).fingerprint != _proof_for_rclone(gcs).fingerprint


def test_fingerprint_includes_capability_set_content() -> None:
    bytes_by_path = _grant(
        kind=StoreKind.HTTP,
        root="https://example.test/alpha/beta/",
        capabilities=[StoreCapability.BYTES_BY_PATH.value],
    )
    byte_range = {
        **bytes_by_path,
        "capabilities": [StoreCapability.BYTE_RANGE.value],
    }

    assert _proof_for_http(bytes_by_path).fingerprint != _proof_for_http(
        byte_range
    ).fingerprint


def test_local_fingerprint_includes_native_anchor_and_ordered_components() -> None:
    baseline = _proof_for_local(_grant(root=_local_path("alpha", "beta"))).fingerprint
    reversed_components = _proof_for_local(
        _grant(root=_local_path("beta", "alpha"))
    ).fingerprint

    assert baseline != reversed_components
    if os.name == "nt":
        other_anchor = _proof_for_local(
            _grant(root=_local_path("alpha", "beta", drive="D"))
        ).fingerprint
        assert baseline != other_anchor


def test_rclone_fingerprint_includes_remote_rootedness_and_credential_mode() -> None:
    baseline = _rclone_grant(
        root="/alpha/beta",
        remote="remote-a",
        credential_mode="name_fallback",
    )
    variants = (
        {**baseline, "remote": "remote-b"},
        {**baseline, "root": "alpha/beta"},
        {**baseline, "root": "/beta/alpha"},
        {**baseline, "credential_mode": "credential_ref"},
    )
    baseline_fingerprint = _proof_for_rclone(baseline).fingerprint

    assert all(
        _proof_for_rclone(variant).fingerprint != baseline_fingerprint for variant in variants
    )


def test_git_fingerprint_includes_every_remote_family_field_and_path_order() -> None:
    baseline = _grant(
        kind=StoreKind.GIT,
        root="ssh://git@example.test/alpha/beta",
    )
    variants = (
        {**baseline, "root": "ssh://other@example.test/alpha/beta"},
        {**baseline, "root": "ssh://git@example.test:2222/alpha/beta"},
        {**baseline, "root": "ssh://git@other.test/alpha/beta"},
        {**baseline, "root": "git@example.test:alpha/beta"},
        {**baseline, "root": "ssh://git@example.test/beta/alpha"},
    )
    baseline_fingerprint = _proof_for_git(baseline).fingerprint

    assert all(_proof_for_git(variant).fingerprint != baseline_fingerprint for variant in variants)


def test_golden_fingerprint_vectors_are_stable() -> None:
    local = _proof_for_local(
        _grant(
            grant_id="ignored-local-id",
            kind=StoreKind.LOCAL_FS,
            root=_local_path("alpha", "beta"),
            capabilities=[StoreCapability.BYTES_BY_PATH.value],
        )
    )
    http = _proof_for_http(
        _grant(
            grant_id="ignored-http-id",
            kind=StoreKind.HTTP,
            root="https://EXAMPLE.test:443/alpha/beta",
            capabilities=[
                StoreCapability.BYTE_RANGE.value,
                StoreCapability.BYTES_BY_PATH.value,
            ],
        )
    )
    rclone = _proof_for_rclone(
        _rclone_grant(
            grant_id="ignored-rclone-id",
            scope=_group_scope(),
            kind=StoreKind.S3,
            root="/alpha/beta",
            capabilities=[
                StoreCapability.VERSIONED_SNAPSHOT.value,
                StoreCapability.BYTES_BY_PATH.value,
            ],
            remote="research-remote",
            credential_mode="credential_ref",
        ),
        scope=GroupStoreScope(GROUP_ID),
    )
    git = _proof_for_git(
        _grant(
            grant_id="ignored-git-id",
            kind=StoreKind.GIT,
            root="ssh://git@example.test:2222/org/repository",
            capabilities=[
                StoreCapability.VERSIONED_SNAPSHOT.value,
                StoreCapability.BYTES_BY_PATH.value,
            ],
        )
    )

    expected_local_fingerprint = (
        "sag-v1-sha256:6559a8b39962fe34ea41a294887a1027e2fca65868f4e2524bed55f1d63f7086"
        if os.name == "nt"
        else "sag-v1-sha256:040e9cd4ddb49af698e100cc78d150d4dd04f189a8bd561c58261c668c7c91ef"
    )
    assert local.fingerprint == expected_local_fingerprint
    assert (
        http.fingerprint
        == "sag-v1-sha256:61558ce8d51299dc2342ad700e01d6c3df472abed4d2ce8a42fbc3e82d9fceb9"
    )
    assert (
        rclone.fingerprint
        == "sag-v1-sha256:d2c48e5741fad954eedb45556798828f4445588fb713cab5db6fb7e4321a6f6b"
    )
    assert (
        git.fingerprint
        == "sag-v1-sha256:d39f55ca6d0b74f891d7d237188402ab9c41de35b99c3ab30994c12b73c8085d"
    )


def _assert_deeply_immutable(value: object, *, seen: set[int]) -> None:
    if value is None or isinstance(value, (str, int, bool, UUID, StoreKind, StoreCapability)):
        return
    if id(value) in seen:
        return
    seen.add(id(value))
    assert not isinstance(value, (dict, list, set, bytearray))
    if isinstance(value, (tuple, frozenset)):
        for item in value:
            _assert_deeply_immutable(item, seen=seen)
        return
    assert is_dataclass(value)
    assert cast(Any, value).__dataclass_params__.frozen
    assert not hasattr(value, "__dict__")
    for field in fields(value):
        _assert_deeply_immutable(getattr(value, field.name), seen=seen)


def test_registry_grants_boundaries_scopes_and_proofs_are_redacted_and_immutable() -> None:
    secret = "operator-secret-root"
    registry = StoreAuthorityRegistry.from_json(
        _raw(
            _grant(
                kind=StoreKind.HTTP,
                root=f"https://example.test/{secret}/",
            ),
            _rclone_grant(
                grant_id="rclone",
                scope=_group_scope(),
                root=f"/{secret}",
            ),
        )
    )

    _assert_deeply_immutable(registry, seen=set())
    objects: list[object] = [registry]
    for grant in registry._grants:
        objects.extend((grant, grant.scope, grant.boundary, grant.proof))
    assert all(secret not in repr(value) for value in objects)


def test_registry_parsing_and_authorization_perform_no_host_or_external_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("authority registry attempted I/O")

    isolated_os = SimpleNamespace(
        name=os.name,
        fsencode=os.fsencode,
        fspath=os.fspath,
        getcwd=unexpected,
        stat=unexpected,
        lstat=unexpected,
        readlink=unexpected,
        open=unexpected,
    )
    monkeypatch.setattr(local_authority_module, "os", isolated_os)
    monkeypatch.setattr(builtins, "open", unexpected)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(subprocess, "check_output", unexpected)

    registry = StoreAuthorityRegistry.from_json(_raw(_grant()))

    assert _authorize(registry) is not None


def test_core_module_has_no_auth_repository_sqlalchemy_or_route_imports() -> None:
    source = (
        Path(__file__).parents[1] / "src" / "lab_tracker" / "store_authority_registry.py"
    ).read_text(encoding="utf-8")

    forbidden = (
        "lab_tracker.auth",
        "lab_tracker.repository",
        "sqlalchemy",
        "lab_tracker.routes",
        "lab_tracker.api",
    )
    assert all(name not in source for name in forbidden)
