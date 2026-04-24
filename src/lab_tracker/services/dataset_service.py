"""Dataset domain service mixin."""

from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetFile,
    DatasetStatus,
    QuestionLink,
    QuestionLinkRole,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _actor_user_id,
    _build_commit_manifest,
    _compute_commit_hash,
    _ensure_dataset_status_transition,
    _ensure_primary_question_active,
    _manifest_input_from_commit,
    _unique_ids,
    _validate_commit_hash,
)


def _load_attached_files(self, dataset_id: UUID) -> list[DatasetFile] | None:
    repository = self._active_repository()
    if repository is None or self._allow_in_memory:
        return None
    return repository.list_dataset_files(dataset_id)


def _load_dataset_note_targets(self, dataset_id: UUID) -> list[UUID] | None:
    repository = self._active_repository()
    if repository is None or self._allow_in_memory:
        return None
    return repository.list_dataset_note_target_ids(dataset_id)


def _merge_unique_ids(base: list[UUID], additions: Iterable[UUID]) -> list[UUID]:
    merged = list(base)
    seen = set(base)
    for value in additions:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return merged


class DatasetServiceMixin:
    def create_dataset(
        self,
        project_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None = None,
        status: DatasetStatus = DatasetStatus.STAGED,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        commit_hash: str | None = None,
        actor: AuthContext | None = None,
    ) -> Dataset:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        if primary_question_id is None:
            raise ValidationError("primary_question_id is required.")
        primary_question = self.get_question(primary_question_id)
        if primary_question.project_id != project_id:
            raise ValidationError("Primary question must belong to the same project.")
        secondary_ids = _unique_ids(secondary_question_ids)
        if primary_question_id in secondary_ids:
            raise ValidationError("Primary question cannot be secondary.")
        for question_id in secondary_ids:
            question = self.get_question(question_id)
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
        resolved_manifest = _build_commit_manifest(
            commit_manifest,
            question_links,
        )
        self._ensure_source_session_valid(resolved_manifest.source_session_id, project_id)
        if status == DatasetStatus.COMMITTED and not resolved_manifest.files:
            raise ValidationError("At least one file is required to commit a dataset.")
        resolved_commit_hash = _compute_commit_hash(resolved_manifest)
        _validate_commit_hash(commit_hash, resolved_commit_hash)

        dataset = Dataset(
            dataset_id=uuid4(),
            project_id=project_id,
            commit_hash=resolved_commit_hash,
            primary_question_id=primary_question_id,
            question_links=question_links,
            commit_manifest=resolved_manifest,
            status=status,
            created_by=_actor_user_id(actor),
        )
        if commit_requested:
            _ensure_primary_question_active(primary_question)
        self._remember_entity("datasets", dataset.dataset_id, dataset)
        self._run_repository_write(lambda repository: repository.datasets.save(dataset))
        return dataset

    def get_dataset(self, dataset_id: UUID) -> Dataset:
        return self._get_from_repository_or_store(
            attribute_name="datasets",
            entity_id=dataset_id,
            label="Dataset",
            loader=lambda repository: repository.datasets.get(dataset_id),
        )

    def list_datasets(self, *, project_id: UUID | None = None) -> list[Dataset]:
        datasets = self._query_from_repository(
            attribute_name="datasets",
            loader=lambda repository: repository.query_datasets(
                project_id=project_id,
                limit=None,
                offset=0,
            ),
            entity_id_getter=lambda dataset: dataset.dataset_id,
        )
        if datasets is not None:
            return datasets
        if project_id is None:
            return list(self._store.datasets.values())
        return [d for d in self._store.datasets.values() if d.project_id == project_id]

    def update_dataset(
        self,
        dataset_id: UUID,
        *,
        status: DatasetStatus | None = None,
        question_links: Iterable[QuestionLink] | None = None,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        commit_hash: str | None = None,
        actor: AuthContext | None = None,
    ) -> Dataset:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        if status is not None:
            _ensure_dataset_status_transition(dataset.status, status)
        was_committed = dataset.status == DatasetStatus.COMMITTED
        if was_committed:
            if commit_hash is not None or question_links is not None or commit_manifest is not None:
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
                question = self.get_question(link.question_id)
                if question.project_id != dataset.project_id:
                    raise ValidationError("Question links must belong to the same project.")
            dataset.question_links = links
            dataset.primary_question_id = primary_links[0].question_id

        commit_requested = (
            status == DatasetStatus.COMMITTED and dataset.status != DatasetStatus.COMMITTED
        )

        if commit_requested:
            primary_question = self.get_question(dataset.primary_question_id)
            _ensure_primary_question_active(primary_question)

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
                if attached_files is None:
                    files = list(base_manifest.files)
                else:
                    files = attached_files
                if not files:
                    raise ValidationError("At least one file is required to commit a dataset.")

                note_ids = list(base_manifest.note_ids)
                note_targets = _load_dataset_note_targets(self, dataset.dataset_id)
                if note_targets:
                    note_ids = _merge_unique_ids(note_ids, note_targets)

                base_manifest = DatasetCommitManifestInput(
                    files=files,
                    metadata=base_manifest.metadata,
                    nwb_metadata=base_manifest.nwb_metadata,
                    bids_metadata=base_manifest.bids_metadata,
                    note_ids=note_ids,
                    source_session_id=base_manifest.source_session_id,
                )

            resolved_manifest = _build_commit_manifest(
                base_manifest,
                dataset.question_links,
            )
            self._ensure_source_session_valid(
                resolved_manifest.source_session_id, dataset.project_id
            )
            resolved_commit_hash = _compute_commit_hash(resolved_manifest)
            _validate_commit_hash(commit_hash, resolved_commit_hash)
            dataset.commit_manifest = resolved_manifest
            dataset.commit_hash = resolved_commit_hash
        else:
            _validate_commit_hash(commit_hash, _compute_commit_hash(dataset.commit_manifest))
        if status is not None:
            dataset.status = status
        dataset.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.datasets.save(dataset))
        return dataset

    def delete_dataset(self, dataset_id: UUID, *, actor: AuthContext | None = None) -> Dataset:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        self._forget_entity("datasets", dataset_id)
        self._run_repository_write(lambda repository: repository.datasets.delete(dataset_id))
        return dataset
