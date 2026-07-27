"""DataStore registry service: register and list data-store locations."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
from typing import cast
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.data_store_definition import (
    DataStoreDefinitionError,
    ValidatedDataStoreDefinition,
)
from lab_tracker.errors import (
    ConflictError,
    DataStorePersistenceError,
    NotFoundError,
    OpaqueTargetNotFoundError,
    StoreAuthorityDeniedError,
    ValidationError,
)
from lab_tracker.models import (
    DataStore,
    StoreCapability,
    StoreKind,
    default_store_capabilities,
)
from lab_tracker.repository import (
    DataStoreForeignKeyRaceError,
    DataStoreInsertError,
    DataStoreNameRaceError,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.shared import actor_user_fk, actor_user_id
from lab_tracker.store_authority_registry import (
    GroupStoreScope,
    ProjectStoreScope,
    StoreAuthorityRegistry,
)

STORE_AUTHORITY_DENIED_MESSAGE = "Data store authority is unavailable."
DATA_STORE_NAME_CONFLICT_MESSAGE = (
    "A data store with this name already exists in the selected scope."
)
DATA_STORE_CONTEXT_CONFLICT_MESSAGE = (
    "Data store registration context changed before it could be saved."
)


class DataStoreService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        authorization: ProjectAuthorizationPolicy,
        store_authority_registry: StoreAuthorityRegistry,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.authorization = authorization
        self.store_authority_registry = store_authority_registry

    def create_data_store(
        self,
        *,
        project_id: UUID | None = None,
        group_id: UUID | None = None,
        name: str,
        kind: StoreKind,
        root: str,
        capabilities: Iterable[StoreCapability] | None = None,
        endpoint: str | None = None,
        credential_ref: str | None = None,
        authority_grant_id: str | None = None,
        is_default: bool = False,
        actor: AuthContext | None = None,
    ) -> DataStore:
        if (project_id is None) == (group_id is None):
            raise ValidationError(
                "A data store must be scoped to exactly one of project_id or group_id."
            )
        authority_scope: ProjectStoreScope | GroupStoreScope
        if project_id is not None:
            self.authorization.require_contributor(project_id, actor=actor)
            self.projects.get_project(project_id)
            authority_scope = ProjectStoreScope(project_id)
        else:
            if group_id is None:  # pragma: no cover - guarded by the XOR above
                raise RuntimeError("Data-store scope invariant was violated.")
            self.authorization.require_group_owner(group_id, actor=actor)
            self.projects.get_project_group(group_id)
            authority_scope = GroupStoreScope(group_id)
        try:
            definition = ValidatedDataStoreDefinition.create(
                name=name,
                kind=kind,
                root=root,
                endpoint=endpoint,
                credential_ref=credential_ref,
            )
        except DataStoreDefinitionError as exc:
            raise ValidationError(str(exc)) from None
        resolved_capabilities = _bounded_effective_capabilities(
            capabilities,
            kind=definition.kind,
        )
        proof = (
            self.store_authority_registry.authorize(
                grant_id=authority_grant_id,
                scope=authority_scope,
                candidate=definition,
                capabilities=resolved_capabilities,
            )
            if authority_grant_id is not None
            else None
        )
        if proof is None:
            raise StoreAuthorityDeniedError(STORE_AUTHORITY_DENIED_MESSAGE)
        store = DataStore(
            store_id=uuid4(),
            project_id=project_id,
            group_id=group_id,
            name=definition.name,
            kind=definition.kind,
            capabilities=list(resolved_capabilities),
            root=definition.root,
            endpoint=definition.endpoint,
            credential_ref=definition.credential_ref,
            authority_grant_id=proof.grant_id,
            authority_grant_fingerprint=proof.fingerprint,
            is_default=is_default,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        mapped_error: ConflictError | DataStorePersistenceError | None = None
        try:
            # Reserve SQLite's single writer only after RBAC, semantic
            # validation, and exact operator-grant authorization have passed.
            # The preparation hook runs before the generic SQLite savepoint,
            # while keeping direct-service failures inside rollback ownership.
            with self.recoverable_unit_of_work(
                prepare=lambda repository: repository.data_stores.reserve_registration_write(),
            ) as repository:
                repository.data_stores.insert(store)
                if is_default:
                    repository.data_stores.clear_default(
                        project_id,
                        group_id=group_id,
                        except_store_id=store.store_id,
                    )
        except DataStoreNameRaceError:
            mapped_error = ConflictError(DATA_STORE_NAME_CONFLICT_MESSAGE)
        except DataStoreForeignKeyRaceError:
            mapped_error = ConflictError(DATA_STORE_CONTEXT_CONFLICT_MESSAGE)
        except DataStoreInsertError:
            mapped_error = DataStorePersistenceError()
        if mapped_error is not None:
            raise mapped_error
        return store

    def get_data_store(self, store_id: UUID) -> DataStore:
        return cast(
            DataStore,
            self.get_from_repository(
                entity_id=store_id,
                label="Data store",
                loader=lambda repository: repository.data_stores.get(store_id),
            ),
        )

    def get_data_store_for_read(
        self,
        store_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> DataStore:
        try:
            store = self.get_data_store(store_id)
        except NotFoundError as exc:
            raise OpaqueTargetNotFoundError("Data store does not exist.") from exc

        if store.project_id is not None:
            can_read = self.authorization.can_read(store.project_id, actor=actor)
        elif store.group_id is not None:
            can_read = self.authorization.can_group_read(store.group_id, actor=actor)
        else:
            can_read = False
        if not can_read:
            raise OpaqueTargetNotFoundError("Data store does not exist.")
        return store

    def list_data_stores(
        self,
        *,
        project_id: UUID | None = None,
        group_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> list[DataStore]:
        if project_id is not None and group_id is not None:
            raise ValidationError("Provide at most one of project_id or group_id.")
        if project_id is not None:
            # Effective stores: the project's own plus those inherited from its group.
            self.authorization.require_read(project_id, actor=actor)
            return self.repository.data_stores.list_effective_for_project(project_id)
        if group_id is not None:
            self.authorization.require_group_read(group_id, actor=actor)
            stores, _ = self.repository.data_stores.query(group_id=group_id)
            return stores
        project_ids = self.authorization.accessible_project_ids(actor)
        if project_ids is None:
            return self.repository.data_stores.list()
        if not project_ids:
            return []
        stores, _ = self.repository.data_stores.query(project_ids=project_ids)
        return stores


def _bounded_effective_capabilities(
    capabilities: Iterable[StoreCapability] | None,
    *,
    kind: StoreKind,
) -> tuple[StoreCapability, ...]:
    if capabilities is None:
        return tuple(default_store_capabilities(kind))
    try:
        values = tuple(islice(iter(capabilities), len(StoreCapability) + 1))
    except Exception:
        raise StoreAuthorityDeniedError(STORE_AUTHORITY_DENIED_MESSAGE) from None
    if len(values) > len(StoreCapability):
        raise StoreAuthorityDeniedError(STORE_AUTHORITY_DENIED_MESSAGE)
    return values
