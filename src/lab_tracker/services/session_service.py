"""Session and acquisition-output service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import NotFoundError, ValidationError
from lab_tracker.models import (
    AcquisitionOutput,
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetStatus,
    EntityOrigin,
    EntityType,
    ExperimentStatus,
    QuestionStatus,
    Session,
    SessionStatus,
    SessionType,
    decode_session_link_code,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.goal_link_cleanup import remove_goal_links_to_entity
from lab_tracker.services.project_authorization import ProjectAuthorizationPolicy
from lab_tracker.services.project_service import ProjectService
from lab_tracker.services.question_service import QuestionService
from lab_tracker.services.shared import (
    _ensure_session_status_transition,
    _find_acquisition_output,
    _manifest_input_with_source,
    _merge_acquisition_outputs,
    actor_user_fk,
    actor_user_id,
    ensure_non_empty,
)

if TYPE_CHECKING:
    from lab_tracker.services.dataset_service import DatasetService
    from lab_tracker.services.experiment_service import ExperimentService


class SessionService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        datasets_provider: Callable[[], DatasetService],
        experiments_provider: Callable[[], ExperimentService] | None = None,
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self._datasets_provider = datasets_provider
        self._experiments_provider = experiments_provider
        self.authorization = authorization

    @property
    def datasets(self) -> DatasetService:
        return self._datasets_provider()

    @property
    def experiments(self) -> ExperimentService | None:
        if self._experiments_provider is None:
            return None
        return self._experiments_provider()

    def _find_existing_acquisition_output(
        self,
        session_id: UUID,
        file_path: str,
    ) -> AcquisitionOutput | None:
        outputs, _ = self.repository.query_acquisition_outputs(
            session_id=session_id,
            limit=None,
            offset=0,
        )
        return _find_acquisition_output(
            {output.output_id: output for output in outputs},
            session_id,
            file_path,
        )

    def create_session(
        self,
        project_id: UUID,
        session_type: SessionType,
        *,
        primary_question_id: UUID | None = None,
        actor: AuthContext | None = None,
        origin: EntityOrigin = EntityOrigin.USER,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Session:
        self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        if session_type == SessionType.SCIENTIFIC:
            if primary_question_id is None:
                raise ValidationError("Scientific sessions require a primary question.")
        elif session_type == SessionType.OPERATIONAL and primary_question_id is not None:
            raise ValidationError("Operational sessions cannot have a primary question.")
        if primary_question_id is not None:
            question = self.questions.get_question(primary_question_id)
            if question.project_id != project_id:
                raise ValidationError("Primary question must belong to the same project.")
            if session_type == SessionType.SCIENTIFIC and question.status != QuestionStatus.ACTIVE:
                raise ValidationError("Primary question must be active for scientific sessions.")
        session = Session(
            session_id=uuid4(),
            project_id=project_id,
            session_type=session_type,
            status=SessionStatus.ACTIVE,
            primary_question_id=primary_question_id,
            created_by=actor_user_id(actor),
            created_by_user_id=actor_user_fk(actor, self.repository),
            origin=origin,
            change_set_id=change_set_id,
            origin_provider=origin_provider,
            origin_model=origin_model,
            origin_prompt_version=origin_prompt_version,
        )
        with self.unit_of_work() as repository:
            repository.sessions.save(session)
        return session

    def get_session(self, session_id: UUID) -> Session:
        return self.get_from_repository(
            entity_id=session_id,
            label="Session",
            loader=lambda repository: repository.sessions.get(session_id),
        )

    def get_session_for_read(
        self,
        session_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Session:
        session = self.get_session(session_id)
        if not self.authorization.can_read(session.project_id, actor=actor):
            raise NotFoundError("Session does not exist.")
        return session

    def get_session_by_link_code(self, link_code: str) -> Session:
        ensure_non_empty(link_code, "link_code")
        try:
            session_id = decode_session_link_code(link_code)
        except ValueError as exc:
            raise ValidationError("Invalid session link code.") from exc
        return self.get_session(session_id)

    def get_session_by_link_code_for_read(
        self,
        link_code: str,
        *,
        actor: AuthContext | None = None,
    ) -> Session:
        session = self.get_session_by_link_code(link_code)
        if not self.authorization.can_read(session.project_id, actor=actor):
            raise NotFoundError("Session does not exist.")
        return session

    def list_sessions(self, *, project_id: UUID | None = None) -> list[Session]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_sessions(
                project_id=project_id,
                limit=None,
                offset=0,
            ),
        )

    def update_session(
        self,
        session_id: UUID,
        *,
        status: PatchValue[SessionStatus | None] = NOT_PROVIDED,
        ended_at: PatchValue[datetime | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
        origin: EntityOrigin | None = None,
        change_set_id: UUID | None = None,
        origin_provider: str | None = None,
        origin_model: str | None = None,
        origin_prompt_version: str | None = None,
    ) -> Session:
        session = self.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        before = session.model_copy(deep=True)
        if is_provided(status):
            if status is None:
                raise ValidationError("status must not be null.")
            next_status = status
            _ensure_session_status_transition(session.status, status)
        else:
            next_status = session.status
        if is_provided(ended_at) and ended_at is not None and next_status != SessionStatus.CLOSED:
            raise ValidationError("ended_at can only be set when closing a session.")
        if is_provided(ended_at) and ended_at is None and next_status == SessionStatus.CLOSED:
            raise ValidationError("ended_at must not be null for a closed session.")
        if is_provided(status):
            session.status = status
        if next_status == SessionStatus.CLOSED:
            session.ended_at = (
                ended_at
                if is_provided(ended_at)
                else session.ended_at or utc_now()
            )
        elif is_provided(ended_at):
            session.ended_at = ended_at
        if origin is not None:
            session.origin = origin
        if change_set_id is not None:
            session.change_set_id = change_set_id
        if origin_provider is not None:
            session.origin_provider = origin_provider
        if origin_model is not None:
            session.origin_model = origin_model
        if origin_prompt_version is not None:
            session.origin_prompt_version = origin_prompt_version
        if session == before:
            return session
        session.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.sessions.save(session)
        return session

    def delete_session(self, session_id: UUID, *, actor: AuthContext | None = None) -> Session:
        session = self.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        self._ensure_session_can_be_deleted(session)
        with self.unit_of_work() as repository:
            remove_goal_links_to_entity(
                repository,
                entity_type=EntityType.SESSION,
                entity_id=session_id,
            )
            repository.sessions.delete(session_id)
        return session

    def _ensure_session_can_be_deleted(self, session: Session) -> None:
        experiments, _ = self.repository.query_experiments(
            session_id=session.session_id,
            limit=None,
            offset=0,
        )
        if experiments:
            raise ValidationError(
                "Session cannot be deleted while Experiments reference it."
            )
        collection_dataset_ids = (
            self.repository.acquisition_collections.dataset_ids_referencing_session(
                session.session_id
            )
        )
        if collection_dataset_ids:
            raise ValidationError(
                "Session cannot be deleted while Datasets reference collection "
                "snapshots captured in it."
            )
        datasets = self.query_from_repository(
            loader=lambda repository: repository.query_datasets(
                project_id=session.project_id,
                limit=None,
                offset=0,
            ),
        )
        for dataset in datasets:
            if (
                dataset.status != DatasetStatus.STAGED
                and dataset.commit_manifest.source_session_id == session.session_id
            ):
                raise ValidationError(
                    "Session cannot be deleted while non-staged datasets reference it."
                )

    def register_acquisition_output(
        self,
        session_id: UUID,
        file_path: str,
        checksum: str,
        *,
        size_bytes: int | None = None,
        actor: AuthContext | None = None,
    ) -> AcquisitionOutput:
        session = self.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        ensure_non_empty(file_path, "file_path")
        ensure_non_empty(checksum, "checksum")
        if size_bytes is not None and size_bytes < 0:
            raise ValidationError("size_bytes must be 0 or greater.")
        cleaned_path = file_path.strip()
        cleaned_checksum = checksum.strip()
        existing = self._find_existing_acquisition_output(session_id, cleaned_path)
        if existing is not None:
            updated = False
            if existing.checksum != cleaned_checksum:
                existing.checksum = cleaned_checksum
                updated = True
            if size_bytes is not None and existing.size_bytes != size_bytes:
                existing.size_bytes = size_bytes
                updated = True
            if updated:
                existing.updated_at = utc_now()
                with self.unit_of_work() as repository:
                    repository.acquisition_outputs.save(existing)
            return existing
        output = AcquisitionOutput(
            output_id=uuid4(),
            session_id=session_id,
            file_path=cleaned_path,
            checksum=cleaned_checksum,
            size_bytes=size_bytes,
        )
        with self.unit_of_work() as repository:
            repository.acquisition_outputs.save(output)
        return output

    def list_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
    ) -> list[AcquisitionOutput]:
        return self.query_from_repository(
            loader=lambda repository: repository.query_acquisition_outputs(
                session_id=session_id,
                limit=None,
                offset=0,
            ),
        )

    def delete_acquisition_output(
        self, output_id: UUID, *, actor: AuthContext | None = None
    ) -> AcquisitionOutput:
        output = self.get_from_repository(
            entity_id=output_id,
            label="Acquisition output",
            loader=lambda repository: repository.acquisition_outputs.get(output_id),
        )
        session = self.get_session(output.session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        with self.unit_of_work() as repository:
            repository.acquisition_outputs.delete(output_id)
        return output

    def promote_operational_session(
        self,
        session_id: UUID,
        primary_question_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Session:
        session = self.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError(
                "Only operational sessions can be promoted to scientific sessions."
            )
        if session.status != SessionStatus.ACTIVE:
            raise ValidationError("Only active operational sessions can be promoted.")
        question = self.questions.get_question(primary_question_id)
        if question.project_id != session.project_id:
            raise ValidationError("Primary question must belong to the same project.")
        if question.status != QuestionStatus.ACTIVE:
            raise ValidationError("Primary question must be active for scientific sessions.")
        linked_experiments, _ = self.repository.query_experiments(
            session_id=session.session_id,
            limit=None,
            offset=0,
        )
        mismatched_experiments = [
            experiment.name
            for experiment in linked_experiments
            if experiment.primary_question_id != primary_question_id
        ]
        if mismatched_experiments:
            names = ", ".join(sorted(mismatched_experiments))
            raise ValidationError(
                "Operational Session cannot become scientific because its "
                f"Experiment questions do not match: {names}"
            )
        session.session_type = SessionType.SCIENTIFIC
        session.primary_question_id = primary_question_id
        session.updated_at = utc_now()
        with self.unit_of_work() as repository:
            repository.sessions.save(session)
        return session

    def promote_operational_session_to_dataset(
        self,
        session_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None = None,
        status: DatasetStatus = DatasetStatus.COMMITTED,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        actor: AuthContext | None = None,
    ) -> Dataset:
        session = self.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        with self.application_transaction():
            # Session acquisition state is the outermost lock scope. Dataset
            # creation and inherited Experiment memberships stay inside this
            # transaction, after a post-lock re-read of all source state.
            self.repository.lock_session_acquisition_state(session_id)
            linked_experiments, _ = self.repository.query_experiments(
                session_id=session_id,
                limit=None,
                offset=0,
            )
            self.repository.lock_experiment_updates(
                experiment.experiment_id
                for experiment in linked_experiments
            )
            return self._promote_operational_session_to_dataset_locked(
                session_id,
                primary_question_id,
                secondary_question_ids=secondary_question_ids,
                status=status,
                commit_manifest=commit_manifest,
                actor=actor,
            )

    def _promote_operational_session_to_dataset_locked(
        self,
        session_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None,
        status: DatasetStatus,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None,
        actor: AuthContext | None,
    ) -> Dataset:
        """Promote after re-reading under Session and linked Experiment locks."""

        session = self.get_session(session_id)
        self.authorization.require_contributor(session.project_id, actor=actor)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
        if session.status != SessionStatus.ACTIVE:
            raise ValidationError("Only active operational sessions can be promoted.")
        linked_experiments, _ = self.repository.query_experiments(
            session_id=session.session_id,
            limit=None,
            offset=0,
        )
        archived_experiment_names = sorted(
            experiment.name
            for experiment in linked_experiments
            if experiment.status == ExperimentStatus.ARCHIVED
        )
        if archived_experiment_names:
            raise ValidationError(
                "Cannot promote a Session linked to archived Experiments: "
                + ", ".join(archived_experiment_names)
            )
        merged_secondary_question_ids = list(secondary_question_ids or [])
        for experiment in linked_experiments:
            if (
                experiment.primary_question_id != primary_question_id
                and experiment.primary_question_id
                not in merged_secondary_question_ids
            ):
                merged_secondary_question_ids.append(
                    experiment.primary_question_id
                )
        collection_snapshot_ids, collection_members = (
            self._current_collection_snapshot_state(session.session_id)
        )
        outputs = self.list_acquisition_outputs(session_id=session.session_id)
        merged_manifest = _merge_acquisition_outputs(commit_manifest, outputs)
        manifest_with_session = _manifest_input_with_source(merged_manifest, session.session_id)
        manifest_with_collections = _manifest_with_collection_snapshots(
            manifest_with_session,
            collection_snapshot_ids,
        )
        reconciled_manifest = _reconcile_manifest_files_with_collections(
            manifest_with_collections,
            collection_members,
        )
        dataset = self.datasets.create_dataset(
            project_id=session.project_id,
            primary_question_id=primary_question_id,
            secondary_question_ids=merged_secondary_question_ids,
            status=status,
            commit_manifest=reconciled_manifest,
            actor=actor,
        )
        if self.experiments is not None:
            for experiment in linked_experiments:
                self.experiments._add_dataset_locked(  # noqa: SLF001
                    experiment.experiment_id,
                    dataset.dataset_id,
                    actor=actor,
                )
        return dataset

    def _current_collection_snapshot_state(
        self,
        session_id: UUID,
    ) -> tuple[list[UUID], dict[str, set[str]]]:
        collections, _ = self.repository.acquisition_collections.query(
            session_id=session_id,
            limit=None,
            offset=0,
        )
        snapshot_ids: list[UUID] = []
        member_checksums: dict[str, set[str]] = {}
        unsealed: list[str] = []
        for collection in collections:
            snapshot = collection.current_snapshot
            if snapshot is None:
                unsealed.append(collection.collection_key)
                continue
            if not snapshot.complete:
                unsealed.append(collection.collection_key)
                continue
            snapshot_ids.append(snapshot.snapshot_id)
            payload = self.repository.acquisition_collections.get_manifest(
                snapshot.snapshot_id
            )
            if payload is None:
                raise ValidationError(
                    "Current collection manifest is missing: "
                    f"{collection.collection_key}"
                )
            for member in payload.get("members", []):
                if not isinstance(member, dict):
                    raise ValidationError(
                        "Current collection manifest is malformed: "
                        f"{collection.collection_key}"
                    )
                path = str(member.get("path", ""))
                checksum = str(member.get("checksum", ""))
                member_checksums.setdefault(path, set()).add(checksum)
        if unsealed:
            names = ", ".join(sorted(unsealed))
            raise ValidationError(
                "Current acquisition collections must be complete before "
                f"Dataset promotion: {names}"
            )
        return snapshot_ids, member_checksums


def _manifest_with_collection_snapshots(
    manifest: DatasetCommitManifestInput,
    snapshot_ids: list[UUID],
) -> DatasetCommitManifestInput:
    return DatasetCommitManifestInput(
        files=manifest.files,
        external_artifacts=manifest.external_artifacts,
        collection_snapshot_ids=snapshot_ids,
        metadata=manifest.metadata,
        nwb_metadata=manifest.nwb_metadata,
        bids_metadata=manifest.bids_metadata,
        note_ids=manifest.note_ids,
        source_session_id=manifest.source_session_id,
    )


def _reconcile_manifest_files_with_collections(
    manifest: DatasetCommitManifestInput,
    collection_members: dict[str, set[str]],
) -> DatasetCommitManifestInput:
    files = []
    for file in manifest.files:
        checksums = collection_members.get(file.path.strip())
        if checksums is None:
            files.append(file)
            continue
        if checksums == {file.checksum.strip()}:
            continue
        raise ValidationError(
            "Acquisition collection checksum conflict for file path: "
            f"{file.path.strip()}"
        )
    return DatasetCommitManifestInput(
        files=files,
        external_artifacts=manifest.external_artifacts,
        collection_snapshot_ids=manifest.collection_snapshot_ids,
        metadata=manifest.metadata,
        nwb_metadata=manifest.nwb_metadata,
        bids_metadata=manifest.bids_metadata,
        note_ids=manifest.note_ids,
        source_session_id=manifest.source_session_id,
    )
