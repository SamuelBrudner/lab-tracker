from __future__ import annotations

import builtins
import json
import socket
import subprocess
from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID

import pytest
from api_helpers import (
    TEST_STORE_AUTHORITY_GRANT_ID,
    ExactCandidateTestStoreAuthority,
)

from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    GroupStoreScope,
    ProjectStoreScope,
    StoreAuthorityProof,
    StoreAuthorityRegistry,
)
from lab_tracker.store_authority_use import (
    DetachedStoreAuthorityBinding,
    FixedStoreAuthoritySnapshotProvider,
    StoreAuthorityBindingIdentity,
    StoreAuthorityUseProof,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
GROUP_ID = UUID("22222222-2222-4222-8222-222222222222")
STORE_ID = UUID("33333333-3333-4333-8333-333333333333")
GRANT_ID = "sensitive-grant-marker"
GRANT_ROOT = "https://files.example/sensitive-grant-root"
STORE_ROOT = f"{GRANT_ROOT}/data"


def _registry(
    *,
    grant_id: str = GRANT_ID,
    project_id: UUID | None = PROJECT_ID,
    group_id: UUID | None = None,
    root: str = GRANT_ROOT,
    capabilities: tuple[StoreCapability, ...] = (StoreCapability.BYTES_BY_PATH,),
) -> StoreAuthorityRegistry:
    scope: dict[str, str]
    if project_id is not None:
        scope = {"project_id": str(project_id)}
    elif group_id is not None:
        scope = {"group_id": str(group_id)}
    else:
        raise AssertionError("test registry needs one scope")
    raw = json.dumps(
        {
            "schema": STORE_AUTHORITY_CONFIG_SCHEMA,
            "grants": [
                {
                    "grant_id": grant_id,
                    "scope": scope,
                    "kind": StoreKind.HTTP.value,
                    "root": root,
                    "capabilities": [capability.value for capability in capabilities],
                }
            ],
        },
        separators=(",", ":"),
    )
    return StoreAuthorityRegistry.from_json(raw)


def _definition(
    *,
    root: str = STORE_ROOT,
) -> ValidatedDataStoreDefinition:
    return ValidatedDataStoreDefinition.create(
        name="approved-http",
        kind=StoreKind.HTTP,
        root=root,
    )


def _store(
    registry: StoreAuthorityRegistry,
    *,
    store_id: UUID = STORE_ID,
    project_id: UUID | None = PROJECT_ID,
    group_id: UUID | None = None,
    capabilities: tuple[StoreCapability, ...] = (StoreCapability.BYTES_BY_PATH,),
    root: str = STORE_ROOT,
) -> DataStore:
    definition = _definition(root=root)
    scope = (
        ProjectStoreScope(project_id)
        if project_id is not None
        else GroupStoreScope(group_id)  # type: ignore[arg-type]
    )
    registry_proof = registry.authorize(
        grant_id=GRANT_ID,
        scope=scope,
        candidate=definition,
        capabilities=capabilities,
    )
    assert registry_proof is not None
    return DataStore(
        store_id=store_id,
        project_id=project_id,
        group_id=group_id,
        name=definition.name,
        kind=definition.kind,
        capabilities=list(capabilities),
        root=definition.root,
        endpoint=definition.endpoint,
        credential_ref=definition.credential_ref,
        authority_grant_id=registry_proof.grant_id,
        authority_grant_fingerprint=registry_proof.fingerprint,
    )


def _replace_unvalidated(store: DataStore, **updates: object) -> DataStore:
    return store.model_copy(update=updates)


def test_fixed_provider_returns_the_exact_startup_snapshot_and_redacts_repr() -> None:
    registry = _registry()

    provider = FixedStoreAuthoritySnapshotProvider(registry)

    assert provider() is registry
    assert provider() is provider()
    assert GRANT_ID not in repr(provider)
    assert GRANT_ROOT not in repr(provider)
    assert not hasattr(provider, "__dict__")
    with pytest.raises(FrozenInstanceError):
        provider._registry = StoreAuthorityRegistry.deny_all()  # type: ignore[misc]
    with pytest.raises(TypeError, match="snapshot is invalid"):
        FixedStoreAuthoritySnapshotProvider(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("scope_kind", ("project", "group"))
def test_detach_and_revalidate_expose_only_exact_read_only_values(
    scope_kind: str,
) -> None:
    if scope_kind == "project":
        registry = _registry()
        store = _store(registry)
        expected_scope = ProjectStoreScope(PROJECT_ID)
    else:
        registry = _registry(project_id=None, group_id=GROUP_ID)
        store = _store(registry, project_id=None, group_id=GROUP_ID)
        expected_scope = GroupStoreScope(GROUP_ID)

    binding = detach_store_authority_binding(store)
    assert binding is not None
    proof = revalidate_store_authority_binding(registry, binding)

    assert proof is not None
    assert proof.store_id == STORE_ID
    assert proof.scope == expected_scope
    assert proof.definition == _definition()
    assert proof.capabilities == (StoreCapability.BYTES_BY_PATH,)
    assert proof.binding_identity is binding.binding_identity
    assert binding.store_id == proof.store_id
    assert binding.scope == proof.scope
    assert binding.definition is proof.definition
    assert binding.capabilities is proof.capabilities
    assert binding.binding_identity.store_id == STORE_ID
    assert binding.binding_identity.grant_id == GRANT_ID
    assert binding.binding_identity.fingerprint == store.authority_grant_fingerprint

    for value in (binding, binding.binding_identity, proof):
        assert not hasattr(value, "__dict__")
        assert GRANT_ID not in repr(value)
        assert GRANT_ROOT not in repr(value)
        assert store.authority_grant_fingerprint not in repr(value)
    with pytest.raises(FrozenInstanceError):
        binding._identity = binding.binding_identity  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        proof._binding = binding  # type: ignore[misc]


def test_binding_identity_covers_the_complete_binding_and_is_hashable() -> None:
    registry = _registry(
        capabilities=(
            StoreCapability.BYTES_BY_PATH,
            StoreCapability.BYTE_RANGE,
        )
    )
    store = _store(
        registry,
        capabilities=(
            StoreCapability.BYTES_BY_PATH,
            StoreCapability.BYTE_RANGE,
        ),
    )

    first = detach_store_authority_binding(store)
    second = detach_store_authority_binding(store.model_copy(deep=True))
    different_store = detach_store_authority_binding(
        _replace_unvalidated(store, store_id=UUID("44444444-4444-4444-8444-444444444444"))
    )

    assert first is not None
    assert second is not None
    assert different_store is not None
    assert first is not second
    assert first.binding_identity == second.binding_identity
    assert hash(first.binding_identity) == hash(second.binding_identity)
    assert first.binding_identity != different_store.binding_identity
    assert {
        first.binding_identity,
        second.binding_identity,
        different_store.binding_identity,
    } == {first.binding_identity, different_store.binding_identity}


def test_sealed_values_reject_public_construction() -> None:
    registry = _registry()
    binding = detach_store_authority_binding(_store(registry))
    assert binding is not None
    registry_proof = registry.authorize(
        grant_id=GRANT_ID,
        scope=binding.scope,
        candidate=binding.definition,
        capabilities=binding.capabilities,
    )
    assert registry_proof is not None
    identity = binding.binding_identity

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


@pytest.mark.parametrize(
    "updates",
    (
        {"store_id": str(STORE_ID)},
        {"project_id": None},
        {"group_id": GROUP_ID},
        {"project_id": str(PROJECT_ID)},
        {"name": " invalid"},
        {"kind": StoreKind.DATABASE},
        {"root": "relative/path"},
        {"endpoint": "https://secret-endpoint.example"},
        {"credential_ref": "secret-credential-ref"},
        {"capabilities": []},
        {"capabilities": tuple([StoreCapability.BYTES_BY_PATH])},
        {
            "capabilities": [
                StoreCapability.BYTES_BY_PATH,
                StoreCapability.BYTES_BY_PATH,
            ]
        },
        {"capabilities": ["bytes_by_path"]},
        {"capabilities": list(StoreCapability) + [StoreCapability.QUERY]},
        {"authority_grant_id": None},
        {"authority_grant_fingerprint": None},
        {"authority_grant_id": "-invalid"},
        {"authority_grant_id": "valid\ninvalid"},
        {"authority_grant_id": "éinvalid"},
        {"authority_grant_fingerprint": f"sag-v1-sha256:{'A' * 64}"},
        {"authority_grant_fingerprint": f"sag-v1-sha256:{'0' * 63}"},
        {"authority_grant_fingerprint": f"sha256:{'0' * 64}"},
    ),
)
def test_detachment_fails_closed_for_malformed_persisted_fields(
    updates: dict[str, object],
) -> None:
    registry = _registry()
    malformed = _replace_unvalidated(_store(registry), **updates)

    assert detach_store_authority_binding(malformed) is None


def test_detachment_rejects_valid_but_noncanonical_persisted_definition() -> None:
    registry = _registry()
    store = _store(registry)
    noncanonical = _replace_unvalidated(
        store,
        root="https://FILES.EXAMPLE:443/sensitive-grant-root/data",
    )

    assert _definition(root=noncanonical.root).root == _definition().root
    assert detach_store_authority_binding(noncanonical) is None


def test_detachment_rejects_non_datastore_without_touching_hostile_attributes() -> None:
    class Hostile:
        def __getattribute__(self, _name: str) -> Any:
            raise AssertionError("hostile attribute was evaluated")

    assert detach_store_authority_binding(Hostile()) is None  # type: ignore[arg-type]


def test_detachment_and_revalidation_are_pure_and_perform_zero_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    store = _store(registry)

    def unexpected_io(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("authority use boundary attempted I/O")

    monkeypatch.setattr(builtins, "open", unexpected_io)
    monkeypatch.setattr(socket, "socket", unexpected_io)
    monkeypatch.setattr(socket, "create_connection", unexpected_io)
    monkeypatch.setattr(subprocess, "run", unexpected_io)
    monkeypatch.setattr(subprocess, "Popen", unexpected_io)

    binding = detach_store_authority_binding(store)
    assert binding is not None
    assert revalidate_store_authority_binding(registry, binding) is not None


def test_revalidation_passes_the_exact_complete_binding_to_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry(
        capabilities=(
            StoreCapability.BYTES_BY_PATH,
            StoreCapability.BYTE_RANGE,
        )
    )
    binding = detach_store_authority_binding(
        _store(
            registry,
            capabilities=(
                StoreCapability.BYTES_BY_PATH,
                StoreCapability.BYTE_RANGE,
            ),
        )
    )
    assert binding is not None
    calls: list[dict[str, object]] = []
    original = StoreAuthorityRegistry.authorize

    def record_authorize(
        self: StoreAuthorityRegistry,
        *,
        grant_id: str,
        scope: object,
        candidate: object,
        capabilities: object,
    ) -> StoreAuthorityProof | None:
        calls.append(
            {
                "self": self,
                "grant_id": grant_id,
                "scope": scope,
                "candidate": candidate,
                "capabilities": capabilities,
            }
        )
        return original(
            self,
            grant_id=grant_id,
            scope=scope,  # type: ignore[arg-type]
            candidate=candidate,  # type: ignore[arg-type]
            capabilities=capabilities,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(StoreAuthorityRegistry, "authorize", record_authorize)

    proof = revalidate_store_authority_binding(registry, binding)

    assert proof is not None
    assert calls == [
        {
            "self": registry,
            "grant_id": binding.binding_identity.grant_id,
            "scope": binding.scope,
            "candidate": binding.definition,
            "capabilities": binding.capabilities,
        }
    ]


def test_revalidation_accepts_the_trusted_exact_candidate_authorizer_seam() -> None:
    authority = ExactCandidateTestStoreAuthority()
    definition = _definition()
    capabilities = (StoreCapability.BYTES_BY_PATH,)
    registry_proof = authority.authorize(
        grant_id=TEST_STORE_AUTHORITY_GRANT_ID,
        scope=ProjectStoreScope(PROJECT_ID),
        candidate=definition,
        capabilities=capabilities,
    )
    assert registry_proof is not None
    store = DataStore(
        store_id=STORE_ID,
        project_id=PROJECT_ID,
        name=definition.name,
        kind=definition.kind,
        capabilities=list(capabilities),
        root=definition.root,
        endpoint=definition.endpoint,
        credential_ref=definition.credential_ref,
        authority_grant_id=registry_proof.grant_id,
        authority_grant_fingerprint=registry_proof.fingerprint,
    )
    binding = detach_store_authority_binding(store)
    assert binding is not None

    proof = revalidate_store_authority_binding(authority, binding)

    assert proof is not None
    assert proof.binding_identity is binding.binding_identity


def test_revalidation_denies_revoked_changed_and_wrong_scope_grants() -> None:
    original = _registry()
    binding = detach_store_authority_binding(_store(original))
    assert binding is not None
    changed = _registry(
        capabilities=(
            StoreCapability.BYTES_BY_PATH,
            StoreCapability.BYTE_RANGE,
        )
    )
    wrong_scope = _registry(project_id=None, group_id=GROUP_ID)

    assert revalidate_store_authority_binding(StoreAuthorityRegistry.deny_all(), binding) is None
    assert revalidate_store_authority_binding(changed, binding) is None
    assert revalidate_store_authority_binding(wrong_scope, binding) is None


def test_revalidation_compares_both_returned_grant_id_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _registry()
    binding = detach_store_authority_binding(_store(original))
    assert binding is not None
    other_id_registry = _registry(grant_id="other-grant")
    other_id_proof = other_id_registry.authorize(
        grant_id="other-grant",
        scope=binding.scope,
        candidate=binding.definition,
        capabilities=binding.capabilities,
    )
    changed_registry = _registry(
        capabilities=(
            StoreCapability.BYTES_BY_PATH,
            StoreCapability.BYTE_RANGE,
        )
    )
    changed_proof = changed_registry.authorize(
        grant_id=GRANT_ID,
        scope=binding.scope,
        candidate=binding.definition,
        capabilities=binding.capabilities,
    )
    assert other_id_proof is not None
    assert changed_proof is not None
    assert other_id_proof.grant_id != binding.binding_identity.grant_id
    assert changed_proof.fingerprint != binding.binding_identity.fingerprint

    monkeypatch.setattr(
        StoreAuthorityRegistry,
        "authorize",
        lambda *_args, **_kwargs: other_id_proof,
    )
    assert revalidate_store_authority_binding(original, binding) is None

    monkeypatch.setattr(
        StoreAuthorityRegistry,
        "authorize",
        lambda *_args, **_kwargs: changed_proof,
    )
    assert revalidate_store_authority_binding(original, binding) is None


def test_revalidation_rejects_wrong_boundary_types() -> None:
    registry = _registry()
    binding = detach_store_authority_binding(_store(registry))
    assert binding is not None

    assert revalidate_store_authority_binding(object(), binding) is None  # type: ignore[arg-type]
    assert revalidate_store_authority_binding(registry, object()) is None  # type: ignore[arg-type]
