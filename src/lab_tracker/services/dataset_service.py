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
    ProjectReviewPolicy,
    QuestionLink,
    QuestionLinkRole,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _actor_user_id,
    _build_commit_manifest,
    _compute_commit_hash,
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
    def _dataset_review_required(self, project_id: UUID) -> bool:
        policy = self.get_project(project_id).review_policy
        return policy == ProjectReviewPolicy.ALL

    def _default_dataset_reviewer_user_id(self, project_id: UUID) -> UUID | None:
        project = self.get_project(project_id)
        if not project.created_by:
            return None
        try:
            return UUID(project.created_by)
        except ValueError:
            return None

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
        review_required = commit_requested and self._dataset_review_required(project_id)
        resolved_status = DatasetStatus.STAGED if review_required else status

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
        if resolved_status == DatasetStatus.COMMITTED and not resolved_manifest.files:
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
            status=resolved_status,
            created_by=_actor_user_id(actor),
        )
        if commit_requested:
            _ensure_primary_question_active(primary_question)
        self._store.datasets[dataset.dataset_id] = dataset
        self._run_repository_write(lambda repository: repository.datasets.save(dataset))
        if review_required:
            self.request_dataset_review(
                dataset.dataset_id,
                reviewer_user_id=self._default_dataset_reviewer_user_id(project_id),
                actor=actor,
            )
        return dataset

    def get_dataset(self, dataset_id: UUID) -> Dataset:
        return self._get_from_repository_or_store(
            attribute_name="datasets",
            entity_id=dataset_id,
            label="Dataset",
            loader=lambda repository: repository.datasets.get(dataset_id),
        )

    def list_datasets(self, *, project_id: UUID | None = None) -> list[Dataset]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            datasets, _ = repository.query_datasets(
                project_id=project_id,
                limit=None,
                offset=0,
            )
            return self._cache_entities(
                "datasets",
                datasets,
                lambda dataset: dataset.dataset_id,
            )
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
        was_committed = dataset.status == DatasetStatus.COMMITTED
        if was_committed:
            if commit_hash is not None or question_links is not None or commit_manifest is not None:
                raise ValidationError("Committed datasets are immutable.")
            if status == DatasetStatus.STAGED:
                raise ValidationError("Committed datasets cannot return to staged.")
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
        review_required = commit_requested and self._dataset_review_required(dataset.project_id)
        is_committing = commit_requested and not review_required

        if commit_requested:
            primary_question = self.get_question(dataset.primary_question_id)
            _ensure_primary_question_active(primary_question)

        should_refresh_manifest = (
            commit_manifest is not None or question_links is not None or is_committing
        )
        if should_refresh_manifest:
            if commit_manifest is None:
                base_manifest = _manifest_input_from_commit(dataset.commit_manifest)
            elif isinstance(commit_manifest, DatasetCommitManifest):
                base_manifest = _manifest_input_from_commit(commit_manifest)
            else:
                base_manifest = commit_manifest

            if is_committing:
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
                    extraction_provenance=base_manifest.extraction_provenance,
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
            if status == DatasetStatus.COMMITTED and dataset.status != DatasetStatus.COMMITTED:
                if self._dataset_review_required(dataset.project_id):
                    self.request_dataset_review(
                        dataset.dataset_id,
                        reviewer_user_id=self._default_dataset_reviewer_user_id(
                            dataset.project_id
                        ),
                        actor=actor,
                    )
                else:
                    dataset.status = status
            else:
                dataset.status = status
        dataset.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.datasets.save(dataset))
        return dataset

    def delete_dataset(self, dataset_id: UUID, *, actor: AuthContext | None = None) -> Dataset:
        require_role(actor, WRITE_ROLES)
        dataset = self.get_dataset(dataset_id)
        self._store.datasets.pop(dataset_id, None)
        self._run_repository_write(lambda repository: repository.datasets.delete(dataset_id))
        return dataset
