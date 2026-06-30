"""DataStore registry service: register and list data-store locations."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ConflictError, ValidationError
from lab_tracker.models import (
    DataStore,
    StoreCapability,
    StoreKind,
    default_store_capabilities,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.shared import actor_user_fk, actor_user_id, ensure_non_empty


class DataStoreService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.authorization = authorization

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
        is_default: bool = False,
        actor: AuthContext | None = None,
    ) -> DataStore:
        if (project_id is None) == (group_id is None):
            raise ValidationError(
                "A data store must be scoped to exactly one of project_id or group_id."
            )
        if project_id is not None:
            self.projects.get_project(project_id)
            self.authorization.require_contributor(project_id, actor=actor)
            scope_label = "project"
        else:
            self.authorization.require_group_owner(group_id, actor=actor)
            scope_label = "group"
        ensure_non_empty(name, "name")
        ensure_non_empty(root, "root")
        resolved_capabilities = (
            list(capabilities) if capabilities is not None else default_store_capabilities(kind)
        )
        store = DataStore(
            store_id=uuid4(),
            project_id=project_id,
            group_id=group_id,
            name=name.strip(),
            kind=kind,
            capabilities=resolved_capabilities,
            root=root.strip(),
            endpoint=endpoint.strip() if endpoint else None,
            credential_ref=credential_ref.strip() if credential_ref else None,
            is_default=is_default,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        with self.unit_of_work() as repository:
            existing = repository.data_stores.scoped_store_by_name(
                project_id=project_id, group_id=group_id, name=store.name
            )
            if existing is not None:
                raise ConflictError(
                    f"A data store named '{store.name}' already exists in this {scope_label}."
                )
            repository.data_stores.save(store)
            if is_default:
                repository.data_stores.clear_default(
                    project_id, group_id=group_id, except_store_id=store.store_id
                )
        return store

    def get_data_store(self, store_id: UUID) -> DataStore:
        return self.get_from_repository(
            entity_id=store_id,
            label="Data store",
            loader=lambda repository: repository.data_stores.get(store_id),
        )

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
