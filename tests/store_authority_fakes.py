"""Shared builders for use-time store-authority tests.

Bead lab-tracker-n5kp.63.4 splits store authority into two moments: a
registration-time grant check, and a use-time revalidation performed after the
database scope is released and before any target, cache, DNS, network,
credential, or subprocess work.  Proving the second moment needs a persisted
row that is *genuinely* bound -- a canonical definition plus the exact grant ID
and fingerprint a real :class:`StoreAuthorityRegistry` would mint -- because
:func:`lab_tracker.store_authority_use.detach_store_authority_binding` fails
closed on anything less.

The builders here mint that row from a real registry rather than hand-writing a
fingerprint, so a test can never accidentally assert against a binding the
production registry would reject.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from uuid import UUID, uuid4

from lab_tracker.data_store_definition import ValidatedDataStoreDefinition
from lab_tracker.models import DataStore, StoreCapability, StoreKind
from lab_tracker.rclone_store_definition import is_rclone_store_kind
from lab_tracker.store_authority_registry import (
    STORE_AUTHORITY_CONFIG_SCHEMA,
    GroupStoreScope,
    ProjectStoreScope,
    StoreAuthorityRegistry,
    StoreAuthorityScope,
)
from lab_tracker.store_authority_use import (
    StoreAuthorityBindingIdentity,
    StoreAuthorityUseProof,
    detach_store_authority_binding,
    revalidate_store_authority_binding,
)
from lab_tracker.store_health import StoreProbeTarget

BOUND_STORE_GRANT_ID = "use-time-authority"


def grant_payload(
    *,
    scope: StoreAuthorityScope,
    definition: ValidatedDataStoreDefinition,
    capabilities: Sequence[StoreCapability],
    grant_id: str = BOUND_STORE_GRANT_ID,
) -> dict[str, object]:
    """Build one operator grant that exactly covers ``definition``."""

    scope_payload: dict[str, object] = (
        {"project_id": str(scope.project_id)}
        if type(scope) is ProjectStoreScope
        else {"group_id": str(scope.group_id)}
    )
    grant: dict[str, object] = {
        "grant_id": grant_id,
        "scope": scope_payload,
        "kind": definition.kind.value,
        "root": definition.root,
        "capabilities": [capability.value for capability in capabilities],
    }
    if is_rclone_store_kind(definition.kind):
        grant["remote"] = definition.credential_ref or definition.name
        grant["credential_mode"] = (
            "credential_ref"
            if definition.credential_ref is not None
            else "name_fallback"
        )
    return grant


def registry_from_grants(grants: Iterable[dict[str, object]]) -> StoreAuthorityRegistry:
    """Build a real immutable registry from explicit operator grants."""

    return StoreAuthorityRegistry.from_json(
        json.dumps(
            {"schema": STORE_AUTHORITY_CONFIG_SCHEMA, "grants": list(grants)},
            separators=(",", ":"),
        )
    )


def empty_registry() -> StoreAuthorityRegistry:
    """Build a real registry that grants nothing -- total revocation."""

    return registry_from_grants(())


def bound_data_store(
    *,
    name: str = "bound-store",
    kind: StoreKind = StoreKind.HTTP,
    root: str = "https://bound.example.test/base/",
    credential_ref: str | None = None,
    capabilities: Sequence[StoreCapability] = (
        StoreCapability.BYTES_BY_PATH,
        StoreCapability.BYTE_RANGE,
    ),
    project_id: UUID | None = None,
    group_id: UUID | None = None,
    grant_id: str = BOUND_STORE_GRANT_ID,
    store_id: UUID | None = None,
) -> tuple[DataStore, StoreAuthorityRegistry, StoreAuthorityScope]:
    """Mint a persisted row that a real registry authorizes at use time.

    Returns the store, a registry that grants exactly it, and its scope.  The
    persisted ``root`` is the canonical form the definition validator produces,
    because ``detach_store_authority_binding`` rejects a row whose stored
    definition is not already canonical.
    """

    if (project_id is None) == (group_id is None):
        raise ValueError("A bound store needs exactly one of project_id/group_id.")
    scope: StoreAuthorityScope = (
        ProjectStoreScope(project_id) if project_id is not None else GroupStoreScope(group_id)
    )
    definition = ValidatedDataStoreDefinition.create(
        name=name,
        kind=kind,
        root=root,
        endpoint=None,
        credential_ref=credential_ref,
    )
    capability_tuple = tuple(capabilities)
    registry = registry_from_grants(
        [
            grant_payload(
                scope=scope,
                definition=definition,
                capabilities=capability_tuple,
                grant_id=grant_id,
            )
        ]
    )
    proof = registry.authorize(
        grant_id=grant_id,
        scope=scope,
        candidate=definition,
        capabilities=capability_tuple,
    )
    if proof is None:  # pragma: no cover - a builder bug, never a test outcome
        raise AssertionError("Test grant does not authorize its own definition.")
    store = DataStore(
        store_id=store_id if store_id is not None else uuid4(),
        project_id=project_id,
        group_id=group_id,
        name=definition.name,
        kind=definition.kind,
        capabilities=list(capability_tuple),
        root=definition.root,
        endpoint=definition.endpoint,
        credential_ref=definition.credential_ref,
        authority_grant_id=proof.grant_id,
        authority_grant_fingerprint=proof.fingerprint,
    )
    return store, registry, scope


class RecordingSnapshotProvider:
    """A use-time snapshot provider that records exactly when it is captured.

    Production wires :class:`FixedStoreAuthoritySnapshotProvider` over the one
    immutable startup registry.  Tests need the same one-capture contract plus
    the ability to observe the capture, so this provider counts calls and
    appends an ordering marker to a shared event log.
    """

    def __init__(
        self,
        registry: StoreAuthorityRegistry,
        *,
        events: list[str] | None = None,
        marker: str = "authority",
    ) -> None:
        self.registry = registry
        self.calls = 0
        self.events = events if events is not None else []
        self._marker = marker

    def __call__(self) -> StoreAuthorityRegistry:
        self.calls += 1
        self.events.append(self._marker)
        return self.registry


class ExplodingSnapshotProvider:
    """A provider that fails the test if the use-time boundary captures it."""

    def __call__(self) -> StoreAuthorityRegistry:  # pragma: no cover - must not run
        raise AssertionError(
            "The authority snapshot was captured on a path that must fail closed."
        )


def bound_authority_proof(**kwargs: object) -> StoreAuthorityUseProof:
    """Mint one real use-time proof for a freshly bound store.

    Target and probe-target factories now require a proof, so tests that only
    need *some* valid authority -- cache-key tests, adapter mechanics tests --
    can take one from here instead of restating the whole binding dance.
    """

    kwargs.setdefault("project_id", uuid4())
    store, registry, _scope = bound_data_store(**kwargs)  # type: ignore[arg-type]
    binding = detach_store_authority_binding(store)
    if binding is None:  # pragma: no cover - a builder bug, never a test outcome
        raise AssertionError("Test store did not detach into a binding.")
    proof = revalidate_store_authority_binding(registry, binding)
    if proof is None:  # pragma: no cover - a builder bug, never a test outcome
        raise AssertionError("Test registry did not revalidate its own binding.")
    return proof


def sealed_binding_identity(**kwargs: object) -> StoreAuthorityBindingIdentity:
    """Return one sealed binding identity for tests of identity values themselves.

    Production probe targets must be built from the complete proof so the
    identity cannot be paired with unrelated adapter fields.
    """

    return bound_authority_proof(**kwargs).binding_identity


def unsafe_probe_target_for_adapter_test(
    *,
    store_id: UUID,
    name: str,
    kind: StoreKind,
    root: str,
    endpoint: str | None,
    credential_ref: str | None,
    authority_binding_identity: StoreAuthorityBindingIdentity | None = None,
) -> StoreProbeTarget:
    """Bypass construction solely to test downstream defensive adapter behavior.

    Python code can always subvert a class with ``object.__new__``. Production
    never does this; the helper is explicit so unit tests can still feed malformed
    legacy-shaped values to an adapter without weakening the normal construction
    API that application code sees.
    """

    target = object.__new__(StoreProbeTarget)
    object.__setattr__(target, "store_id", store_id)
    object.__setattr__(target, "name", name)
    object.__setattr__(target, "kind", kind)
    object.__setattr__(target, "root", root)
    object.__setattr__(target, "endpoint", endpoint)
    object.__setattr__(target, "credential_ref", credential_ref)
    object.__setattr__(
        target,
        "authority_binding_identity",
        authority_binding_identity or sealed_binding_identity(),
    )
    return target
