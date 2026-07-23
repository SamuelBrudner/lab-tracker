"""Dataset domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    EntityOrigin,
    EntityType,
    QuestionLink,
    QuestionLinkRole,
    SessionType,
    utc_now,
)
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_entity
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import (
    _ensure_dataset_status_transition,
    _manifest_input_from_commit,
    actor_user_fk,
    actor_user_id,
    build_commit_manifest,
    compute_commit_hash,
    ensure_primary_question_active,
    terminal_reason_for_status,
    unique_ids,
    validate_commit_hash,
)

if TYPE_CHECKING:
    from lab_tracker.services.session_service import SessionService


def _load_attached_files(self, dataset_id: UUID) -> list[DatasetFile] | None:
    return self.repository.list_dataset_files(dataset_id)


def _load_dataset_note_targets(self, dataset_id: UUID) -> list[UUID] | None:
    return self.repository.list_dataset_note_target_ids(dataset_id)


def _merge_unique_ids(base: list[UUID], additions: Iterable[UUID]) -> list[UUID]:
    merged = list(base)
    seen = set(base)
    for value in additions:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


def _merge_manifest_files_with_attached_files(
    manifest_files: Iterable[DatasetFile],
    attached_files: Iterable[DatasetFile] | None,
) -> list[DatasetFile]:
    merged = list(manifest_files)
    seen = {file.path.strip(): file.checksum.strip() for file in merged}
    for file in attached_files or []:
        path = file.path.strip()
        checksum = file.checksum.strip()
        existing = seen.get(path)
        if existing is None:
            merged.append(DatasetFile(path=path, checksum=checksum))
            seen[path] = checksum
            continue
        if existing != checksum:
            raise ValidationError("Attached file checksum conflict for file path.")
    return merged


class DatasetService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        sessions_provider: Callable[[], SessionService],
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self._sessions_provider = sessions_provider
        self.authorization = authorization

    @property
    def sessions(self) -> SessionService:
        return self._sessions_provider()

    def create_dataset(
        self,
        project_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None = None,
        status: DatasetStatus = DatasetStatus.STAGED,
        terminal_reason: str | None = None,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        commit_hash: str | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Dataset:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        if primary_question_id is None:
            raise ValidationError("primary_question_id is required.")
        primary_question = self.questions.get_question(primary_question_id)
        if primary_question.project_id != project_id:
            raise ValidationError("Primary question must belong to the same project.")
        secondary_ids = unique_ids(secondary_question_ids)
        if primary_question_id in secondary_ids:
            raise ValidationError("Primary question cannot be secondary.")
        for question_id in secondary_ids:
            question = self.questions.get_question(question_id)
            if question.project_id != project_id:
                raise ValidationError("Secondary questions must belong to the same project.")

        commit_requested = status == DatasetStatus.COMMITTED

        question_links = [
            QuestionLink(question_id=primary_question_id, role=QuestionLinkRole.PRIMARY),
            *[
                QuestionLink(question_id=question_id, role=QuestionLinkRole.SECONDARY)
                for question_id in secondary_ids
            ],
        ]
        resolved_manifest = build_commit_manifest(
            commit_manifest,
            question_links,
        )
        self.validate_source_session(resolved_manifest.source_session_id, project_id)
        if (
            status == DatasetStatus.COMMITTED
            and not resolved_manifest.files
            and not resolved_manifest.external_artifacts
        ):
            raise ValidationError(
                "At least one file or external artifact is required to commit a dataset."
            )
        resolved_commit_hash = compute_commit_hash(resolved_manifest)
        validate_commit_hash(commit_hash, resolved_commit_hash)
        resolved_terminal_reason = terminal_reason_for_status(
            None,
            status,
            DatasetStatus.ARCHIVED,
            terminal_reason,
            entity_name="Dataset",
        )

        dataset = Dataset(
            dataset_id=uuid4(),
            project_id=project_id,
            commit_hash=resolved_commit_hash,
            primary_question_id=primary_question_id,
            question_links=question_links,
            commit_manifest=resolved_manifest,
            status=status,
            terminal_reason=resolved_terminal_reason,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        if commit_requested:
            ensure_primary_question_active(primary_question)
        with self.unit_of_work() as repository:
            repository.datasets.save(dataset)
        return dataset

    def get_dataset(self, dataset_id: UUID) -> Dataset:
        return self.get_from_repository(
            entity_id=dataset_id,
            label="Dataset",
            loader=lambda repository: repository.datasets.get(dataset_id),
        )

    def list_datasets(self, *, project_id: UUID | None = None) -> list[Dataset]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_datasets(
                project_id=project_id,
                limit=None,
                offset=0,
            ),
        )

    def update_dataset(
        self,
        dataset_id: UUID,
        *,
        status: DatasetStatus | None = None,
        terminal_reason: str | None = None,
        question_links: Iterable[QuestionLink] | None = None,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        commit_hash: str | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Dataset:
        dataset = self.get_dataset(dataset_id)
        self.authorization.require_contributor(dataset.project_id, actor=actor)
        current_status = dataset.status
        next_status = status or current_status
        if status is not None:
            _ensure_dataset_status_transition(current_status, status)
        resolved_terminal_reason = terminal_reason_for_status(
            current_status,
            next_status,
            DatasetStatus.ARCHIVED,
            terminal_reason,
            entity_name="Dataset",
        )
        was_committed = current_status == DatasetStatus.COMMITTED
        if was_committed and (
            commit_hash is not None or question_links is not None or commit_manifest is not None
        ):
            raise ValidationError("Committed datasets are immutable.")
        if question_links is not None:
            links = list(question_links)
            primary_links = [link for link in links if link.role == QuestionLinkRole.PRIMARY]
            if len(primary_links) != 1:
                raise ValidationError("Dataset must have exactly one primary question link.")
            seen: set[UUID] = set()
            for link in links:
                if link.question_id in seen:
                    raise ValidationError("Duplicate question link.")
                seen.add(link.question_id)
                question = self.questions.get_question(link.question_id)
                if question.project_id != dataset.project_id:
                    raise ValidationError("Question links must belong to the same project.")
            dataset.question_links = links
            dataset.primary_question_id = primary_links[0].question_id

        commit_requested = (
            status == DatasetStatus.COMMITTED and dataset.status != DatasetStatus.COMMITTED
        )

        if commit_requested:
            primary_question = self.questions.get_question(dataset.primary_question_id)
            ensure_primary_question_active(primary_question)

        should_refresh_manifest = (
            commit_manifest is not None or question_links is not None or commit_requested
        )
        if should_refresh_manifest:
            if commit_manifest is None:
                base_manifest = _manifest_input_from_commit(dataset.commit_manifest)
            elif isinstance(commit_manifest, DatasetCommitManifest):
                base_manifest = _manifest_input_from_commit(commit_manifest)
            else:
                base_manifest = commit_manifest

            if commit_requested:
                attached_files = _load_attached_files(self, dataset.dataset_id)
                files = _merge_manifest_files_with_attached_files(
                    base_manifest.files,
                    attached_files,
                )

                note_ids = list(base_manifest.note_ids)
                note_targets = _load_dataset_note_targets(self, dataset.dataset_id)
                if note_targets:
                    note_ids = _merge_unique_ids(note_ids, note_targets)

                base_manifest = DatasetCommitManifestInput(
                    files=files,
                    external_artifacts=base_manifest.external_artifacts,
                    metadata=base_manifest.metadata,
                    nwb_metadata=base_manifest.nwb_metadata,
                    bids_metadata=base_manifest.bids_metadata,
                    note_ids=note_ids,
                    source_session_id=base_manifest.source_session_id,
                )

            resolved_manifest = build_commit_manifest(
                base_manifest,
                dataset.question_links,
            )
            self.validate_source_session(
                resolved_manifest.source_session_id, dataset.project_id
            )
            if (
                commit_requested
                and not resolved_manifest.files
                and not resolved_manifest.external_artifacts
            ):
                raise ValidationError(
                    "At least one file or external artifact is required to commit a dataset."
                )
            resolved_commit_hash = compute_commit_hash(resolved_manifest)
            validate_commit_hash(commit_hash, resolved_commit_hash)
            dataset.commit_manifest = resolved_manifest
            dataset.commit_hash = resolved_commit_hash
        else:
            validate_commit_hash(commit_hash, compute_commit_hash(dataset.commit_manifest))
        if status is not None:
            dataset.status = status
        if resolved_terminal_reason is not None:
            dataset.terminal_reason = resolved_terminal_reason
        if origin is not None:
            dataset.origin = origin
        if change_set_id is not None:
            dataset.change_set_id = change_set_id
        if origin_provider is not None:
            dataset.origin_provider = origin_provider
        if origin_model is not None:
            dataset.origin_model = origin_model
        if origin_prompt_version is not None:
            dataset.origin_prompt_version = origin_prompt_version
        dataset.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.datasets.save(dataset)
        return dataset

    def delete_dataset(self, dataset_id: UUID, *, actor: AuthContext | None = None) -> Dataset:
        dataset = self.get_dataset(dataset_id)
        self.authorization.require_contributor(dataset.project_id, actor=actor)
        self._ensure_dataset_can_be_deleted(dataset)
        with self.unit_of_work() as repository:
            remove_goal_links_to_entity(
                repository,
                entity_type=EntityType.DATASET,
                entity_id=dataset_id,
            )
            repository.datasets.delete(dataset_id)
        return dataset

    def _ensure_dataset_can_be_deleted(self, dataset: Dataset) -> None:
        if dataset.status != DatasetStatus.STAGED:
            raise ValidationError(
                "Only staged, unreferenced datasets can be deleted; archive committed datasets."
            )
        claims = self.query_from_repository(
            loader=lambda repository: repository.query_claims(
                dataset_id=dataset.dataset_id,
                limit=None,
                offset=0,
            ),
        )
        if claims:
            raise ValidationError("Dataset cannot be deleted while claims reference it.")
        analyses = self.query_from_repository(
            loader=lambda repository: repository.query_analyses(
                dataset_id=dataset.dataset_id,
                limit=None,
                offset=0,
            ),
        )
        if analyses:
            raise ValidationError("Dataset cannot be deleted while analyses reference it.")

    def validate_source_session(
        self, source_session_id: UUID | None, project_id: UUID
    ) -> None:
        """Validate an optional dataset source session without writing."""
        if source_session_id is None:
            return
        session = self.sessions.get_session(source_session_id)
        if session.project_id != project_id:
            raise ValidationError("Source session must belong to the same project.")
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
