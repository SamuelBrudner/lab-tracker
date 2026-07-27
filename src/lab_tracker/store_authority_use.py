"""Pure use-time revalidation for persisted data-store authority bindings.

Registration proves that a store matched one operator grant at write time.
Before any cache, credential, filesystem, network, or subprocess work, callers
use this module to detach the persisted binding from repository state and prove
that the exact binding is still authorized by one immutable registry snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lab_tracker.data_store_definition import (
    DataStoreDefinitionError,
    ValidatedDataStoreDefinition,
)
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.store_authority_registry import (
    GroupStoreScope,
    ProjectStoreScope,
    StoreAuthorityProof,
    StoreAuthorityRegistry,
    StoreAuthorityScope,
)

_GRANT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_GRANT_FINGERPRINT_PATTERN = re.compile(r"sag-v1-sha256:[0-9a-f]{64}")


class StoreAuthoritySnapshotProvider(Protocol):
    """Return the one authority snapshot to use for an outbound operation."""

    def __call__(self) -> StoreAuthorityRegistry:
        """Return an immutable registry snapshot."""


class StoreAuthorityAuthorizer(Protocol):
    """Narrow structural authority port used by the revalidation boundary."""

    def authorize(
        self,
        *,
        grant_id: str,
        scope: StoreAuthorityScope,
        candidate: ValidatedDataStoreDefinition,
        capabilities: Iterable[StoreCapability],
    ) -> StoreAuthorityProof | None:
        """Authorize one exact detached binding."""


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class FixedStoreAuthoritySnapshotProvider:
    """Return the exact immutable registry captured at process startup."""

    _registry: StoreAuthorityRegistry

    def __init__(self, registry: StoreAuthorityRegistry) -> None:
        if type(registry) is not StoreAuthorityRegistry:
            raise TypeError("Store authority registry snapshot is invalid.")
        object.__setattr__(self, "_registry", registry)

    def __call__(self) -> StoreAuthorityRegistry:
        """Return the exact startup snapshot without copying or re-parsing it."""

        return self._registry


_BINDING_IDENTITY_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False)
class StoreAuthorityBindingIdentity:
    """Hashable identity of every input covered by one persisted binding."""

    _store_id: UUID
    _scope: StoreAuthorityScope
    _definition: ValidatedDataStoreDefinition
    _capabilities: tuple[StoreCapability, ...]
    _grant_id: str
    _fingerprint: str

    def __init__(
        self,
        *,
        store_id: UUID,
        scope: StoreAuthorityScope,
        definition: ValidatedDataStoreDefinition,
        capabilities: tuple[StoreCapability, ...],
        grant_id: str,
        fingerprint: str,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _BINDING_IDENTITY_FACTORY_TOKEN:
            raise TypeError("StoreAuthorityBindingIdentity must be factory-built.")
        object.__setattr__(self, "_store_id", store_id)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_definition", definition)
        object.__setattr__(self, "_capabilities", capabilities)
        object.__setattr__(self, "_grant_id", grant_id)
        object.__setattr__(self, "_fingerprint", fingerprint)

    @property
    def store_id(self) -> UUID:
        """Return the exact persisted store identifier."""

        return self._store_id

    @property
    def grant_id(self) -> str:
        """Return the exact persisted authority grant identifier."""

        return self._grant_id

    @property
    def fingerprint(self) -> str:
        """Return the exact persisted grant fingerprint."""

        return self._fingerprint


_DETACHED_BINDING_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class DetachedStoreAuthorityBinding:
    """A repository-independent, fully validated authority binding."""

    _identity: StoreAuthorityBindingIdentity

    def __init__(
        self,
        identity: StoreAuthorityBindingIdentity,
        *,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _DETACHED_BINDING_FACTORY_TOKEN:
            raise TypeError("DetachedStoreAuthorityBinding must be factory-built.")
        object.__setattr__(self, "_identity", identity)

    @property
    def store_id(self) -> UUID:
        """Return the exact persisted store identifier."""

        return self._identity._store_id

    @property
    def scope(self) -> StoreAuthorityScope:
        """Return the exact project or group scope."""

        return self._identity._scope

    @property
    def definition(self) -> ValidatedDataStoreDefinition:
        """Return the canonical detached store definition."""

        return self._identity._definition

    @property
    def capabilities(self) -> tuple[StoreCapability, ...]:
        """Return all persisted capabilities in their stored order."""

        return self._identity._capabilities

    @property
    def binding_identity(self) -> StoreAuthorityBindingIdentity:
        """Return the safe hashable identity for cache and target isolation."""

        return self._identity


_USE_PROOF_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class StoreAuthorityUseProof:
    """Proof that one detached binding matches one current registry snapshot."""

    _binding: DetachedStoreAuthorityBinding
    _registry_proof: StoreAuthorityProof

    def __init__(
        self,
        *,
        binding: DetachedStoreAuthorityBinding,
        registry_proof: StoreAuthorityProof,
        _factory_token: object,
    ) -> None:
        if _factory_token is not _USE_PROOF_FACTORY_TOKEN:
            raise TypeError("StoreAuthorityUseProof must be factory-built.")
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_registry_proof", registry_proof)

    @property
    def store_id(self) -> UUID:
        """Return the exact persisted store identifier."""

        return self._binding.store_id

    @property
    def scope(self) -> StoreAuthorityScope:
        """Return the exact project or group scope."""

        return self._binding.scope

    @property
    def definition(self) -> ValidatedDataStoreDefinition:
        """Return the exact canonical store definition."""

        return self._binding.definition

    @property
    def capabilities(self) -> tuple[StoreCapability, ...]:
        """Return the complete persisted capability request."""

        return self._binding.capabilities

    @property
    def binding_identity(self) -> StoreAuthorityBindingIdentity:
        """Return the identity callers must include in targets and cache keys."""

        return self._binding.binding_identity


def detach_store_authority_binding(
    store: DataStore,
) -> DetachedStoreAuthorityBinding | None:
    """Purely detach one exact, canonical, fully bound store or fail closed."""

    if type(store) is not DataStore:
        return None

    store_id = store.store_id
    project_id = store.project_id
    group_id = store.group_id
    if (
        type(store_id) is not UUID
        or (project_id is None) == (group_id is None)
        or (project_id is not None and type(project_id) is not UUID)
        or (group_id is not None and type(group_id) is not UUID)
    ):
        return None
    scope: StoreAuthorityScope
    if project_id is not None:
        scope = ProjectStoreScope(project_id)
    elif group_id is not None:
        scope = GroupStoreScope(group_id)
    else:  # pragma: no cover - guarded by the XOR check above
        return None

    name = store.name
    kind = store.kind
    root = store.root
    endpoint = store.endpoint
    credential_ref = store.credential_ref
    if (
        type(name) is not str
        or type(kind) is not StoreKind
        or type(root) is not str
        or endpoint is not None
        or (
            credential_ref is not None
            and type(credential_ref) is not str
        )
    ):
        return None
    try:
        definition = ValidatedDataStoreDefinition.create(
            name=name,
            kind=kind,
            root=root,
            endpoint=endpoint,
            credential_ref=credential_ref,
        )
    except DataStoreDefinitionError:
        return None
    if (
        definition.name != name
        or definition.kind is not kind
        or definition.root != root
        or definition.endpoint is not endpoint
        or definition.credential_ref != credential_ref
    ):
        return None

    raw_capabilities = store.capabilities
    if (
        type(raw_capabilities) is not list
        or not raw_capabilities
        or len(raw_capabilities) > len(StoreCapability)
        or any(type(capability) is not StoreCapability for capability in raw_capabilities)
    ):
        return None
    capabilities = tuple(raw_capabilities)
    if len(frozenset(capabilities)) != len(capabilities):
        return None

    grant_id = store.authority_grant_id
    fingerprint = store.authority_grant_fingerprint
    if (
        type(grant_id) is not str
        or type(fingerprint) is not str
        or _GRANT_ID_PATTERN.fullmatch(grant_id) is None
        or _GRANT_FINGERPRINT_PATTERN.fullmatch(fingerprint) is None
    ):
        return None

    identity = StoreAuthorityBindingIdentity(
        store_id=store_id,
        scope=scope,
        definition=definition,
        capabilities=capabilities,
        grant_id=grant_id,
        fingerprint=fingerprint,
        _factory_token=_BINDING_IDENTITY_FACTORY_TOKEN,
    )
    return DetachedStoreAuthorityBinding(
        identity,
        _factory_token=_DETACHED_BINDING_FACTORY_TOKEN,
    )


def revalidate_store_authority_binding(
    registry: StoreAuthorityAuthorizer,
    binding: DetachedStoreAuthorityBinding,
) -> StoreAuthorityUseProof | None:
    """Revalidate the exact detached binding against one registry snapshot."""

    if type(binding) is not DetachedStoreAuthorityBinding:
        return None
    identity = binding.binding_identity
    try:
        authorize = registry.authorize
        registry_proof = authorize(
            grant_id=identity._grant_id,
            scope=binding.scope,
            candidate=binding.definition,
            capabilities=binding.capabilities,
        )
    except (AttributeError, TypeError):
        return None
    if (
        type(registry_proof) is not StoreAuthorityProof
        or registry_proof.grant_id != identity._grant_id
        or registry_proof.fingerprint != identity._fingerprint
    ):
        return None
    return StoreAuthorityUseProof(
        binding=binding,
        registry_proof=registry_proof,
        _factory_token=_USE_PROOF_FACTORY_TOKEN,
    )
