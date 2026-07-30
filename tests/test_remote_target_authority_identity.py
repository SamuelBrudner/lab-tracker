from __future__ import annotations

import builtins
import socket
import subprocess
from collections.abc import Callable
from dataclasses import fields
from typing import Any
from uuid import UUID

import pytest
from store_authority_fakes import bound_data_store

import lab_tracker.artifact_resolution as artifact_resolution
from lab_tracker.artifact_resolution import (
    GitStoreResolutionTarget,
    HttpStoreResolutionTarget,
    LocalStoreResolutionTarget,
    RcloneStoreResolutionTarget,
    git_store_resolution_target,
    http_store_resolution_target,
    local_store_resolution_target,
    rclone_store_resolution_target,
)
from lab_tracker.git_store_locator import PinnedGitPath
from lab_tracker.local_store_locator import (
    LocalStoreLocator,
    PortableStorePath,
    canonical_store_uri,
)
from lab_tracker.models import (
    DataStore,
    ExternalArtifactReference,
    StoreCapability,
    StoreKind,
)
from lab_tracker.store_authority_registry import StoreAuthorityRegistry
from lab_tracker.store_authority_use import (
    DetachedStoreAuthorityBinding,
    StoreAuthorityBindingIdentity,
    StoreAuthorityUseProof,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
GROUP_ID = UUID("22222222-2222-4222-8222-222222222222")
STORE_ID = UUID("33333333-3333-4333-8333-333333333333")
OTHER_STORE_ID = UUID("44444444-4444-4444-8444-444444444444")
CREDENTIAL_REF = "must-not-appear-anywhere"
CONTENT_HASH = f"sha256:{'a' * 64}"
GIT_OBJECT_ID = "b" * 40

HTTP_NAME = "bound-http"
HTTP_ROOT = "https://bound.example.test/base/"
HTTP_LOCATOR = "runs/001/frame.bin"
RCLONE_NAME = "bound-rclone"
RCLONE_ROOT = "experiments"
RCLONE_LOCATOR = "001/sample.fcs"
GIT_NAME = "bound-git"
GIT_ROOT = "https://example.com/org/repo.git"
GIT_LOCATOR = f"analysis/run.py@{GIT_OBJECT_ID}"
LOCAL_NAME = "bound-local"
LOCAL_ROOT = "/srv/bound-local-root"
LOCAL_LOCATOR = "runs/001/frame.bin"

StoreFactory = Callable[[], "tuple[DataStore, StoreAuthorityRegistry]"]
TargetFactory = Callable[["DataStore | StoreAuthorityUseProof"], Any]


def _http_store() -> tuple[DataStore, StoreAuthorityRegistry]:
    store, registry, _ = bound_data_store(
        name=HTTP_NAME,
        kind=StoreKind.HTTP,
        root=HTTP_ROOT,
        project_id=PROJECT_ID,
        store_id=STORE_ID,
    )
    return store, registry


def _rclone_store() -> tuple[DataStore, StoreAuthorityRegistry]:
    store, registry, _ = bound_data_store(
        name=RCLONE_NAME,
        kind=StoreKind.ONEDRIVE,
        root=RCLONE_ROOT,
        credential_ref=CREDENTIAL_REF,
        project_id=PROJECT_ID,
        store_id=STORE_ID,
    )
    return store, registry


def _git_store() -> tuple[DataStore, StoreAuthorityRegistry]:
    store, registry, _ = bound_data_store(
        name=GIT_NAME,
        kind=StoreKind.GIT,
        root=GIT_ROOT,
        project_id=PROJECT_ID,
        store_id=STORE_ID,
    )
    return store, registry


def _local_store() -> tuple[DataStore, StoreAuthorityRegistry]:
    store, registry, _ = bound_data_store(
        name=LOCAL_NAME,
        kind=StoreKind.LOCAL_FS,
        root=LOCAL_ROOT,
        project_id=PROJECT_ID,
        store_id=STORE_ID,
    )
    return store, registry


def _portable_reference(
    store_name: str, path: str
) -> tuple[PortableStorePath, ExternalArtifactReference]:
    locator = PortableStorePath.parse_decoded(path)
    assert locator is not None
    uri = canonical_store_uri(store_name, locator)
    assert uri is not None
    return locator, ExternalArtifactReference(
        source_system="store",
        uri=uri,
        content_hash=CONTENT_HASH,
        store_name=store_name,
        locator=locator.path,
    )


def _http_target(source: DataStore | StoreAuthorityUseProof) -> HttpStoreResolutionTarget | None:
    locator, logical_reference = _portable_reference(HTTP_NAME, HTTP_LOCATOR)
    return http_store_resolution_target(
        source,
        locator=locator,
        logical_reference=logical_reference,
    )


def _rclone_target(
    source: DataStore | StoreAuthorityUseProof,
) -> RcloneStoreResolutionTarget | None:
    locator, logical_reference = _portable_reference(RCLONE_NAME, RCLONE_LOCATOR)
    return rclone_store_resolution_target(
        source,
        locator=locator,
        logical_reference=logical_reference,
    )


def _git_target(source: DataStore | StoreAuthorityUseProof) -> GitStoreResolutionTarget | None:
    pin = PinnedGitPath.parse_decoded(GIT_LOCATOR)
    assert pin is not None
    logical_reference = ExternalArtifactReference.for_git_store(
        store_name=GIT_NAME,
        repository_path=pin.path.path,
        object_id=pin.object_id.value,
        content_hash=CONTENT_HASH,
    )
    return git_store_resolution_target(
        source,
        pin=pin,
        logical_reference=logical_reference,
    )


def _local_target(source: DataStore | StoreAuthorityUseProof) -> LocalStoreResolutionTarget | None:
    locator = LocalStoreLocator.parse_decoded(LOCAL_LOCATOR)
    assert locator is not None
    logical_reference = ExternalArtifactReference.for_local_store(
        store_name=LOCAL_NAME,
        locator=locator.path,
        content_hash=CONTENT_HASH,
    )
    return local_store_resolution_target(
        source,
        locator=locator,
        logical_reference=logical_reference,
    )


REMOTE_TARGET_CASES = (
    pytest.param(_http_store, _http_target, id="http"),
    pytest.param(_rclone_store, _rclone_target, id="rclone"),
    pytest.param(_git_store, _git_target, id="git"),
)
TARGET_CASES = REMOTE_TARGET_CASES + (
    pytest.param(_local_store, _local_target, id="local-fs"),
)


TARGET_FACTORY_TOKENS = {
    LocalStoreResolutionTarget: artifact_resolution._LOCAL_STORE_TARGET_FACTORY_TOKEN,
    HttpStoreResolutionTarget: artifact_resolution._HTTP_STORE_TARGET_FACTORY_TOKEN,
    RcloneStoreResolutionTarget: artifact_resolution._RCLONE_STORE_TARGET_FACTORY_TOKEN,
    GitStoreResolutionTarget: artifact_resolution._GIT_STORE_TARGET_FACTORY_TOKEN,
}


def _structural_arguments(target: Any) -> dict[str, object]:
    """Return every constructor argument of ``target`` except its identity."""

    return {
        field.name: getattr(target, field.name)
        for field in fields(type(target))
        if field.name != "authority_binding_identity"
    }


def _use_proof(
    store: DataStore, registry: StoreAuthorityRegistry
) -> StoreAuthorityUseProof:
    binding = detach_store_authority_binding(store)
    assert binding is not None
    proof = revalidate_store_authority_binding(registry, binding)
    assert proof is not None
    return proof


def _replace_unvalidated(store: DataStore, **updates: object) -> DataStore:
    return store.model_copy(update=updates)


def _binding_identity(store: DataStore) -> StoreAuthorityBindingIdentity:
    binding = detach_store_authority_binding(store)
    assert binding is not None
    return binding.binding_identity


def _assert_identity_separates(store: DataStore, updates: dict[str, object]) -> None:
    base = _binding_identity(store)
    mutated = _binding_identity(_replace_unvalidated(store, **updates))

    assert mutated != base
    assert hash(mutated) != hash(base)


@pytest.mark.parametrize(("make_store", "build_target"), REMOTE_TARGET_CASES)
def test_remote_targets_carry_the_exact_use_time_binding_identity(
    make_store: StoreFactory,
    build_target: TargetFactory,
) -> None:
    store, registry = make_store()
    proof = _use_proof(store, registry)

    target = build_target(proof)

    assert target is not None
    assert target.authority_binding_identity is proof.binding_identity


@pytest.mark.parametrize(("make_store", "build_target"), TARGET_CASES)
def test_targets_never_expose_the_sealed_grant_material(
    make_store: StoreFactory,
    build_target: TargetFactory,
) -> None:
    store, registry = make_store()
    proof = _use_proof(store, registry)
    grant_id = store.authority_grant_id
    fingerprint = store.authority_grant_fingerprint
    assert grant_id is not None
    assert fingerprint is not None

    target = build_target(proof)

    assert target is not None
    for rendered in (repr(target), str(target)):
        assert grant_id not in rendered
        assert fingerprint not in rendered
        assert "authority_binding_identity" not in rendered


@pytest.mark.parametrize(("make_store", "build_target"), TARGET_CASES)
def test_sealed_authority_values_never_expose_root_or_credentials(
    make_store: StoreFactory,
    build_target: TargetFactory,
) -> None:
    store, registry = make_store()
    binding = detach_store_authority_binding(store)
    assert binding is not None
    proof = revalidate_store_authority_binding(registry, binding)
    assert proof is not None
    target = build_target(proof)
    assert target is not None
    secrets = [store.authority_grant_id, store.authority_grant_fingerprint, store.root]
    if store.credential_ref is not None:
        secrets.append(store.credential_ref)

    sealed = (binding, binding.binding_identity, proof, target.authority_binding_identity)

    for value in sealed:
        assert not hasattr(value, "__dict__")
        for rendered in (repr(value), str(value)):
            for secret in secrets:
                assert secret is not None
                assert secret not in rendered


def test_identity_is_hashable_and_equal_by_value_for_the_same_binding() -> None:
    store, _ = _http_store()

    first = _binding_identity(store)
    second = _binding_identity(store.model_copy(deep=True))

    assert first is not second
    assert first == second
    assert hash(first) == hash(second)
    assert {first, second} == {first}


@pytest.mark.parametrize(
    "updates",
    (
        pytest.param({"authority_grant_id": "other-grant-id"}, id="grant-id-only"),
        pytest.param(
            {"authority_grant_fingerprint": f"sag-v1-sha256:{'c' * 64}"},
            id="fingerprint-only",
        ),
    ),
)
def test_grant_id_and_fingerprint_each_separate_target_identity(
    updates: dict[str, object],
) -> None:
    store, _ = _http_store()

    _assert_identity_separates(store, updates)


@pytest.mark.parametrize(
    "updates",
    (
        pytest.param({"store_id": OTHER_STORE_ID}, id="store-id"),
        pytest.param({"project_id": None, "group_id": GROUP_ID}, id="scope"),
        pytest.param({"name": "other-http"}, id="definition-name"),
        pytest.param({"root": "https://bound.example.test/other/"}, id="definition-root"),
        pytest.param({"capabilities": [StoreCapability.BYTES_BY_PATH]}, id="capability-set"),
        pytest.param(
            {
                "capabilities": [
                    StoreCapability.BYTE_RANGE,
                    StoreCapability.BYTES_BY_PATH,
                ]
            },
            id="capability-order",
        ),
    ),
)
def test_identity_covers_every_field_of_the_persisted_binding(
    updates: dict[str, object],
) -> None:
    store, _ = _http_store()

    _assert_identity_separates(store, updates)


@pytest.mark.parametrize(
    "updates",
    (
        pytest.param({"kind": StoreKind.S3}, id="definition-kind"),
        pytest.param({"credential_ref": "other-remote"}, id="definition-credential-ref"),
        pytest.param({"credential_ref": None}, id="definition-credential-ref-cleared"),
    ),
)
def test_identity_covers_kind_and_credential_ref_of_the_persisted_binding(
    updates: dict[str, object],
) -> None:
    store, _ = _rclone_store()

    _assert_identity_separates(store, updates)


@pytest.mark.parametrize(("make_store", "build_target"), TARGET_CASES)
def test_targets_cannot_be_built_without_a_use_time_authority_proof(
    make_store: StoreFactory,
    build_target: TargetFactory,
) -> None:
    store, registry = make_store()

    with pytest.raises(
        ValueError,
        match=r"^Store resolution target requires a use-time authority proof\.$",
    ):
        build_target(store)

    proof_target = build_target(_use_proof(store, registry))

    assert proof_target is not None
    assert proof_target.authority_binding_identity is not None


@pytest.mark.parametrize(("make_store", "build_target"), TARGET_CASES)
def test_target_constructors_reject_an_unsealed_binding_identity(
    make_store: StoreFactory,
    build_target: TargetFactory,
) -> None:
    store, registry = make_store()
    proof = _use_proof(store, registry)
    target = build_target(proof)
    assert target is not None
    target_type = type(target)
    structural = _structural_arguments(target)
    factory_token = TARGET_FACTORY_TOKENS[target_type]

    with pytest.raises(TypeError):
        target_type(**structural, _factory_token=factory_token)
    for unsealed in (None, object(), "sag-v1-sha256:forged"):
        with pytest.raises(
            ValueError,
            match=r"^Store resolution target requires a sealed authority identity\.$",
        ):
            target_type(
                **structural,
                authority_binding_identity=unsealed,
                _factory_token=factory_token,
            )

    rebuilt = target_type(
        **structural,
        authority_binding_identity=proof.binding_identity,
        _factory_token=factory_token,
    )

    assert rebuilt == target


def test_sealed_authority_types_reject_forged_construction() -> None:
    store, registry = _http_store()
    binding = detach_store_authority_binding(store)
    assert binding is not None
    proof = revalidate_store_authority_binding(registry, binding)
    assert proof is not None
    identity = binding.binding_identity
    registry_proof = registry.authorize(
        grant_id=identity.grant_id,
        scope=binding.scope,
        candidate=binding.definition,
        capabilities=binding.capabilities,
    )
    assert registry_proof is not None
    assert proof.binding_identity is identity

    with pytest.raises(TypeError, match="must be factory-built"):
        StoreAuthorityBindingIdentity(
            store_id=identity.store_id,
            scope=binding.scope,
            definition=binding.definition,
            capabilities=binding.capabilities,
            grant_id=identity.grant_id,
            fingerprint=identity.fingerprint,
            _factory_token=object(),
        )
    with pytest.raises(TypeError, match="must be factory-built"):
        DetachedStoreAuthorityBinding(identity, _factory_token=object())
    with pytest.raises(TypeError, match="must be factory-built"):
        StoreAuthorityUseProof(
            binding=binding,
            registry_proof=registry_proof,
            _factory_token=object(),
        )


@pytest.mark.parametrize(("make_store", "build_target"), TARGET_CASES)
def test_target_construction_from_a_use_proof_performs_zero_io(
    make_store: StoreFactory,
    build_target: TargetFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, registry = make_store()
    proof = _use_proof(store, registry)

    def unexpected_io(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("target construction attempted I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(socket, "socket", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)
    monkeypatch.setattr(subprocess, "run", unexpected_io)
    monkeypatch.setattr(subprocess, "Popen", unexpected_io)

    target = build_target(proof)

    assert target is not None
    assert target.authority_binding_identity is proof.binding_identity
