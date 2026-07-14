"""Capture context preset service."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import CaptureContext, EntityRef, EntityType, utc_now
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.shared import actor_user_fk, actor_user_id, ensure_non_empty


class CaptureContextService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        authorization: ProjectAuthorizationPolicy,
        target_validator: Callable[[EntityRef, UUID], None],
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.authorization = authorization
        self.target_validator = target_validator

    def create_capture_context(
        self,
        *,
        project_id: UUID,
        label: str,
        site_label: str | None = None,
        place_label: str | None = None,
        default_hint: str | None = None,
        default_targets: Iterable[EntityRef] | None = None,
        actor: AuthContext | None = None,
    ) -> CaptureContext:
        self.projects.get_project(project_id)
        self.authorization.require_contributor(project_id, actor=actor)
        context = CaptureContext(
            capture_context_id=uuid4(),
            project_id=project_id,
            label=_clean_required(label, "label", max_length=150),
            site_label=_clean_optional(site_label, "site_label", max_length=150),
            place_label=_clean_optional(place_label, "place_label", max_length=150),
            default_hint=_clean_optional(default_hint, "default_hint", max_length=500),
            default_targets=self._normalize_default_targets(project_id, default_targets),
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
        )
        with self.unit_of_work() as repository:
            repository.capture_contexts.save(context)
        return context

    def get_capture_context(
        self,
        capture_context_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> CaptureContext:
        context = self.get_from_repository(
            entity_id=capture_context_id,
            label="Capture context",
            loader=lambda repository: repository.capture_contexts.get(capture_context_id),
        )
        self.authorization.require_read(context.project_id, actor=actor)
        return context

    def list_capture_contexts(
        self,
        *,
        project_id: UUID | None = None,
        include_revoked: bool = False,
        actor: AuthContext | None = None,
    ) -> list[CaptureContext]:
        if project_id is not None:
            self.authorization.require_read(project_id, actor=actor)
            contexts, _ = self.repository.query_capture_contexts(
                project_id=project_id,
                include_revoked=include_revoked,
            )
            return contexts
        project_ids = self.authorization.accessible_project_ids(actor)
        if project_ids is None:
            contexts, _ = self.repository.query_capture_contexts(
                include_revoked=include_revoked,
            )
            return contexts
        if not project_ids:
            return []
        contexts, _ = self.repository.query_capture_contexts(
            project_ids=project_ids,
            include_revoked=include_revoked,
        )
        return contexts

    def update_capture_context(
        self,
        capture_context_id: UUID,
        *,
        updates: Mapping[str, object],
        actor: AuthContext | None = None,
    ) -> CaptureContext:
        context = self.get_capture_context(capture_context_id, actor=actor)
        self.authorization.require_contributor(context.project_id, actor=actor)
        if context.revoked_at is not None:
            raise ValidationError("Revoked capture contexts cannot be updated.")
        allowed = {
            "label",
            "site_label",
            "place_label",
            "default_hint",
            "default_targets",
        }
        unknown = set(updates) - allowed
        if unknown:
            fields = ", ".join(sorted(unknown))
            raise ValidationError(f"Unsupported capture context fields: {fields}")
        if "label" in updates:
            raw_label = updates["label"]
            if raw_label is None:
                raise ValidationError("label must not be empty.")
            context.label = _clean_required(str(raw_label), "label", max_length=150)
        if "site_label" in updates:
            context.site_label = _clean_optional(
                _optional_string(updates["site_label"]),
                "site_label",
                max_length=150,
            )
        if "place_label" in updates:
            context.place_label = _clean_optional(
                _optional_string(updates["place_label"]),
                "place_label",
                max_length=150,
            )
        if "default_hint" in updates:
            context.default_hint = _clean_optional(
                _optional_string(updates["default_hint"]),
                "default_hint",
                max_length=500,
            )
        if "default_targets" in updates:
            raw_targets = updates["default_targets"]
            if raw_targets is not None and not isinstance(raw_targets, list):
                raise ValidationError("default_targets must be a list.")
            context.default_targets = self._normalize_default_targets(
                context.project_id,
                raw_targets,
            )
        context.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.capture_contexts.save(context)
        return context

    def revoke_capture_context(
        self,
        capture_context_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> CaptureContext:
        context = self.get_capture_context(capture_context_id, actor=actor)
        self.authorization.require_contributor(context.project_id, actor=actor)
        if context.revoked_at is None:
            context.revoked_at = utc_now()
            context.updated_at = context.revoked_at
            with self.unit_of_work() as repository:
                repository.capture_contexts.save(context)
        return context

    def _normalize_default_targets(
        self,
        project_id: UUID,
        targets: Iterable[EntityRef | Mapping[str, object]] | None,
    ) -> list[EntityRef]:
        try:
            resolved = [EntityRef.model_validate(target) for target in (targets or [])]
        except Exception as exc:
            raise ValidationError("default_targets contains invalid entity refs.") from exc
        for target in resolved:
            if target.entity_type == EntityType.PROJECT and target.entity_id != project_id:
                raise ValidationError(
                    "Default project target must match the capture context project."
                )
            self.target_validator(target, project_id)
        return resolved


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _clean_required(value: str, field_name: str, *, max_length: int) -> str:
    ensure_non_empty(value, field_name)
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ValidationError(f"{field_name} must be {max_length} characters or fewer.")
    return cleaned


def _clean_optional(value: str | None, field_name: str, *, max_length: int) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_length:
        raise ValidationError(f"{field_name} must be {max_length} characters or fewer.")
    return cleaned
