"""Session and acquisition-output service."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AcquisitionOutput,
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetStatus,
    EntityOrigin,
    EntityType,
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


class SessionService(BaseService):
    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: ProjectService,
        questions: QuestionService,
        datasets_provider: Callable[[], DatasetService],
        authorization: ProjectAuthorizationPolicy,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.questions = questions
        self._datasets_provider = datasets_provider
        self.authorization = authorization

    @property
    def datasets(self) -> DatasetService:
        return self._datasets_provider()

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

    def get_session_by_link_code(self, link_code: str) -> Session:
        ensure_non_empty(link_code, "link_code")
        try:
            session_id = decode_session_link_code(link_code)
        except ValueError as exc:
            raise ValidationError("Invalid session link code.") from exc
        return self.get_session(session_id)

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
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
        if session.status != SessionStatus.ACTIVE:
            raise ValidationError("Only active operational sessions can be promoted.")
        outputs = self.list_acquisition_outputs(session_id=session.session_id)
        merged_manifest = _merge_acquisition_outputs(commit_manifest, outputs)
        manifest_with_session = _manifest_input_with_source(merged_manifest, session.session_id)
        return self.datasets.create_dataset(
            project_id=session.project_id,
            primary_question_id=primary_question_id,
            secondary_question_ids=secondary_question_ids,
            status=status,
            commit_manifest=manifest_with_session,
            actor=actor,
        )
