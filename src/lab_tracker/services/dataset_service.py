"""Dataset domain service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.collection_models import (
    DatasetCollectionSnapshotReference,
    snapshot_with_capture_observation,
)
from lab_tracker.errors import NotFoundError, OpaqueTargetNotFoundError, ValidationError
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
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_entity
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import (
    _ensure_dataset_status_transition,
    _manifest_input_from_commit,
    _merge_normalized_dataset_file,
    _normalize_dataset_file,
    _normalize_dataset_files,
    actor_user_fk,
    actor_user_id,
    build_commit_manifest,
    compute_commit_hash,
    ensure_primary_question_active,
    terminal_reason_for_patch,
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
    merged = _normalize_dataset_files(manifest_files)
    file_indexes = {file.path: index for index, file in enumerate(merged)}
    for file in attached_files or []:
        candidate = _normalize_dataset_file(file)
        _merge_normalized_dataset_file(
            merged,
            file_indexes,
            candidate,
            checksum_conflict_message="Attached file checksum conflict for file path.",
            size_conflict_message="Attached file size conflict for file path.",
        )
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
            collection_snapshots=self._resolve_collection_snapshots(
                _collection_snapshot_ids(commit_manifest),
                project_id=project_id,
                source_session_id=_manifest_source_session_id(commit_manifest),
                require_complete=commit_requested,
            ),
        )
        self.validate_source_session(resolved_manifest.source_session_id, project_id)
        if (
            status == DatasetStatus.COMMITTED
            and not resolved_manifest.files
            and not resolved_manifest.external_artifacts
            and not resolved_manifest.collection_snapshots
        ):
            raise ValidationError(
                "At least one file or external artifact is required to commit "
                "a dataset. A complete collection snapshot also satisfies "
                "this requirement."
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

    def get_dataset_for_read(
        self,
        dataset_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Dataset:
        try:
            dataset = self.get_dataset(dataset_id)
        except NotFoundError as exc:
            raise OpaqueTargetNotFoundError("Dataset does not exist.") from exc
        if not self.authorization.can_read(dataset.project_id, actor=actor):
            raise OpaqueTargetNotFoundError("Dataset does not exist.")
        return dataset

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
        status: PatchValue[DatasetStatus | None] = NOT_PROVIDED,
        terminal_reason: PatchValue[str | None] = NOT_PROVIDED,
        question_links: PatchValue[Iterable[QuestionLink] | None] = NOT_PROVIDED,
        commit_manifest: PatchValue[
            DatasetCommitManifestInput | DatasetCommitManifest | None
        ] = NOT_PROVIDED,
        commit_hash: PatchValue[str | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Dataset:
        with self.application_transaction():
            located_dataset = self.get_dataset(dataset_id)
            project_id = located_dataset.project_id
            self.authorization.require_contributor(project_id, actor=actor)
            self.repository.lock_dataset_updates(project_id, (dataset_id,))

            # Dataset persistence writes a complete snapshot, including the
            # provenance manifest and hash. Never mutate locator state after a
            # lock wait: PostgreSQL expires the identity map so this read and
            # every subsequent validation observe the winning file/status
            # transaction.
            dataset = self.get_dataset(dataset_id)
            return self._update_dataset_in_transaction(
                dataset,
                status=status,
                terminal_reason=terminal_reason,
                question_links=question_links,
                commit_manifest=commit_manifest,
                commit_hash=commit_hash,
                actor=actor,
                origin=origin,
                change_set_id=change_set_id,
                origin_provider=origin_provider,
                origin_model=origin_model,
                origin_prompt_version=origin_prompt_version,
            )

    def _update_dataset_in_transaction(
        self,
        dataset: Dataset,
        *,
        status: PatchValue[DatasetStatus | None],
        terminal_reason: PatchValue[str | None],
        question_links: PatchValue[Iterable[QuestionLink] | None],
        commit_manifest: PatchValue[DatasetCommitManifestInput | DatasetCommitManifest | None],
        commit_hash: PatchValue[str | None],
        actor: AuthContext | None,
        origin: EntityOrigin | None,
        change_set_id: UUID | None,
        origin_provider: str | None,
        origin_model: str | None,
        origin_prompt_version: str | None,
    ) -> Dataset:
        before = dataset.model_copy(deep=True)
        current_status = dataset.status
        if is_provided(status):
            if status is None:
                raise ValidationError("status must not be null.")
            next_status = status
            _ensure_dataset_status_transition(current_status, status)
        else:
            next_status = current_status
        resolved_terminal_reason = terminal_reason_for_patch(
            current_status,
            next_status,
            DatasetStatus.ARCHIVED,
            terminal_reason,
            entity_name="Dataset",
        )
        if is_provided(question_links) and question_links is None:
            raise ValidationError("question_links must not be null.")
        if is_provided(commit_manifest) and commit_manifest is None:
            raise ValidationError("commit_manifest must not be null.")
        if is_provided(commit_hash) and commit_hash is None:
            raise ValidationError("commit_hash must not be null.")
        was_committed = current_status == DatasetStatus.COMMITTED
        if was_committed and (
            is_provided(commit_hash) or is_provided(question_links) or is_provided(commit_manifest)
        ):
            raise ValidationError("Committed datasets are immutable.")
        if is_provided(question_links):
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
            parent_experiments, _ = self.repository.query_experiments(
                dataset_id=dataset.dataset_id,
                limit=None,
                offset=0,
            )
            missing_experiments = [
                experiment.name
                for experiment in parent_experiments
                if not any(link.question_id == experiment.primary_question_id for link in links)
            ]
            if missing_experiments:
                names = ", ".join(sorted(missing_experiments))
                raise ValidationError(
                    f"Dataset must retain every parent Experiment question: {names}"
                )
            dataset.question_links = links
            dataset.primary_question_id = primary_links[0].question_id

        commit_requested = (
            is_provided(status)
            and status == DatasetStatus.COMMITTED
            and dataset.status != DatasetStatus.COMMITTED
        )

        if commit_requested:
            primary_question = self.questions.get_question(dataset.primary_question_id)
            ensure_primary_question_active(primary_question)

        should_refresh_manifest = (
            is_provided(commit_manifest) or is_provided(question_links) or commit_requested
        )
        if should_refresh_manifest:
            if not is_provided(commit_manifest):
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
                    collection_snapshot_ids=(base_manifest.collection_snapshot_ids),
                    metadata=base_manifest.metadata,
                    nwb_metadata=base_manifest.nwb_metadata,
                    bids_metadata=base_manifest.bids_metadata,
                    note_ids=note_ids,
                    source_session_id=base_manifest.source_session_id,
                )

            resolved_manifest = build_commit_manifest(
                base_manifest,
                dataset.question_links,
                collection_snapshots=self._resolve_collection_snapshots(
                    base_manifest.collection_snapshot_ids,
                    project_id=dataset.project_id,
                    source_session_id=base_manifest.source_session_id,
                    require_complete=commit_requested,
                ),
            )
            self.validate_source_session(resolved_manifest.source_session_id, dataset.project_id)
            if (
                commit_requested
                and not resolved_manifest.files
                and not resolved_manifest.external_artifacts
                and not resolved_manifest.collection_snapshots
            ):
                raise ValidationError(
                    "At least one file or external artifact is required to "
                    "commit a dataset. A complete collection snapshot also "
                    "satisfies this requirement."
                )
            resolved_commit_hash = compute_commit_hash(resolved_manifest)
            validate_commit_hash(
                commit_hash if is_provided(commit_hash) else None,
                resolved_commit_hash,
            )
            dataset.commit_manifest = resolved_manifest
            dataset.commit_hash = resolved_commit_hash
        elif is_provided(commit_hash):
            validate_commit_hash(commit_hash, compute_commit_hash(dataset.commit_manifest))
        if is_provided(status):
            dataset.status = status
        if is_provided(resolved_terminal_reason):
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
        if dataset == before:
            return dataset
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
        experiments, _ = self.repository.query_experiments(
            dataset_id=dataset.dataset_id,
            limit=None,
            offset=0,
        )
        if experiments:
            raise ValidationError("Dataset cannot be deleted while Experiments reference it.")
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

    def validate_source_session(self, source_session_id: UUID | None, project_id: UUID) -> None:
        """Validate an optional dataset source session without writing."""
        if source_session_id is None:
            return
        session = self.sessions.get_session(source_session_id)
        if session.project_id != project_id:
            raise ValidationError("Source session must belong to the same project.")
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")

    def _resolve_collection_snapshots(
        self,
        snapshot_ids: Iterable[UUID],
        *,
        project_id: UUID,
        source_session_id: UUID | None,
        require_complete: bool,
    ) -> list[DatasetCollectionSnapshotReference]:
        resolved: list[DatasetCollectionSnapshotReference] = []
        incomplete_keys: list[str] = []
        for snapshot_id in unique_ids(snapshot_ids):
            snapshot = self.repository.acquisition_collections.get_snapshot(snapshot_id)
            if snapshot is None:
                raise ValidationError(f"Collection snapshot does not exist: {snapshot_id}")
            collection = self.repository.acquisition_collections.get(snapshot.collection_id)
            if collection is None:
                raise ValidationError(f"Collection does not exist for snapshot: {snapshot_id}")
            session = self.sessions.get_session(collection.session_id)
            if session.project_id != project_id:
                raise ValidationError(
                    "Collection snapshots must belong to the same project as the Dataset."
                )
            if source_session_id is not None and collection.session_id != source_session_id:
                raise ValidationError(
                    "Collection snapshots must belong to the Dataset source Session."
                )
            if require_complete and not snapshot.complete:
                incomplete_keys.append(collection.collection_key)
            capture = None
            if (
                collection.current_snapshot_id == snapshot.snapshot_id
                and collection.current_capture_id is not None
            ):
                capture = self.repository.acquisition_collections.get_capture_by_id(
                    collection.current_capture_id
                )
            if capture is None:
                capture = self.repository.acquisition_collections.get_latest_capture_for_snapshot(
                    snapshot.snapshot_id
                )
            observed_snapshot = snapshot_with_capture_observation(
                snapshot,
                capture,
            )
            resolved.append(
                DatasetCollectionSnapshotReference(
                    snapshot_id=snapshot.snapshot_id,
                    collection_id=collection.collection_id,
                    collection_key=collection.collection_key,
                    manifest_hash=snapshot.manifest_hash,
                    member_count=snapshot.member_count,
                    total_size_bytes=snapshot.total_size_bytes,
                    source_provider=observed_snapshot.source_provider,
                    source_uri=observed_snapshot.source_uri,
                    observed_at=observed_snapshot.observed_at,
                    client_capture_id=observed_snapshot.client_capture_id,
                    complete=observed_snapshot.complete,
                    capture_actor_user_id=(observed_snapshot.capture_actor_user_id),
                    capture_principal_type=(observed_snapshot.capture_principal_type),
                    capture_principal_instance_id=(observed_snapshot.capture_principal_instance_id),
                    capture_principal_label=(observed_snapshot.capture_principal_label),
                )
            )
        if incomplete_keys:
            names = ", ".join(sorted(set(incomplete_keys)))
            raise ValidationError(
                f"Collection snapshots must be complete before Dataset commit: {names}"
            )
        return resolved


def _collection_snapshot_ids(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
) -> list[UUID]:
    if manifest is None:
        return []
    if isinstance(manifest, DatasetCommitManifest):
        return [snapshot.snapshot_id for snapshot in manifest.collection_snapshots]
    return list(manifest.collection_snapshot_ids)


def _manifest_source_session_id(
    manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
) -> UUID | None:
    return None if manifest is None else manifest.source_session_id
