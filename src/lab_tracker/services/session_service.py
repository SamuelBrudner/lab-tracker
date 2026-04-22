"""Session and acquisition-output service mixin."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable
from uuid import UUID, uuid4

from lab_tracker.auth import AuthContext, require_role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    AcquisitionOutput,
    Dataset,
    DatasetCommitManifest,
    DatasetCommitManifestInput,
    DatasetStatus,
    QuestionStatus,
    Session,
    SessionStatus,
    SessionType,
    decode_session_link_code,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _actor_user_id,
    _ensure_non_empty,
    _ensure_session_status_transition,
    _find_acquisition_output,
    _manifest_input_with_source,
    _merge_acquisition_outputs,
)


class SessionServiceMixin:
    def _find_existing_acquisition_output(
        self,
        session_id: UUID,
        file_path: str,
    ) -> AcquisitionOutput | None:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            outputs, _ = repository.query_acquisition_outputs(
                session_id=session_id,
                limit=None,
                offset=0,
            )
            return _find_acquisition_output(
                {output.output_id: output for output in outputs},
                session_id,
                file_path,
            )
        return _find_acquisition_output(self._store.acquisition_outputs, session_id, file_path)

    def create_session(
        self,
        project_id: UUID,
        session_type: SessionType,
        *,
        primary_question_id: UUID | None = None,
        status: SessionStatus = SessionStatus.ACTIVE,
        actor: AuthContext | None = None,
    ) -> Session:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        if session_type == SessionType.SCIENTIFIC:
            if primary_question_id is None:
                raise ValidationError("Scientific sessions require a primary question.")
        elif session_type == SessionType.OPERATIONAL:
            if primary_question_id is not None:
                raise ValidationError("Operational sessions cannot have a primary question.")
        if primary_question_id is not None:
            question = self.get_question(primary_question_id)
            if question.project_id != project_id:
                raise ValidationError("Primary question must belong to the same project.")
            if session_type == SessionType.SCIENTIFIC and question.status != QuestionStatus.ACTIVE:
                raise ValidationError("Primary question must be active for scientific sessions.")
        session = Session(
            session_id=uuid4(),
            project_id=project_id,
            session_type=session_type,
            status=status,
            primary_question_id=primary_question_id,
            created_by=_actor_user_id(actor),
        )
        self._store.sessions[session.session_id] = session
        self._run_repository_write(lambda repository: repository.sessions.save(session))
        return session

    def get_session(self, session_id: UUID) -> Session:
        return self._get_from_repository_or_store(
            attribute_name="sessions",
            entity_id=session_id,
            label="Session",
            loader=lambda repository: repository.sessions.get(session_id),
        )

    def get_session_by_link_code(self, link_code: str) -> Session:
        _ensure_non_empty(link_code, "link_code")
        try:
            session_id = decode_session_link_code(link_code)
        except ValueError as exc:
            raise ValidationError("Invalid session link code.") from exc
        return self.get_session(session_id)

    def list_sessions(self, *, project_id: UUID | None = None) -> list[Session]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            sessions, _ = repository.query_sessions(project_id=project_id, limit=None, offset=0)
            return self._cache_entities(
                "sessions",
                sessions,
                lambda session: session.session_id,
            )
        if project_id is None:
            return list(self._store.sessions.values())
        return [s for s in self._store.sessions.values() if s.project_id == project_id]

    def update_session(
        self,
        session_id: UUID,
        *,
        status: SessionStatus | None = None,
        ended_at: datetime | None = None,
        actor: AuthContext | None = None,
    ) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        next_status = status or session.status
        if status is not None:
            _ensure_session_status_transition(session.status, status)
        if ended_at is not None and next_status != SessionStatus.CLOSED:
            raise ValidationError("ended_at can only be set when closing a session.")
        if status is not None:
            session.status = status
        if next_status == SessionStatus.CLOSED:
            session.ended_at = ended_at or session.ended_at or utc_now()
        elif ended_at is not None:
            session.ended_at = ended_at
        session.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.sessions.save(session))
        return session

    def delete_session(self, session_id: UUID, *, actor: AuthContext | None = None) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        self._store.sessions.pop(session_id, None)
        self._run_repository_write(lambda repository: repository.sessions.delete(session_id))
        return session

    def register_acquisition_output(
        self,
        session_id: UUID,
        file_path: str,
        checksum: str,
        *,
        size_bytes: int | None = None,
        actor: AuthContext | None = None,
    ) -> AcquisitionOutput:
        require_role(actor, WRITE_ROLES)
        self.get_session(session_id)
        _ensure_non_empty(file_path, "file_path")
        _ensure_non_empty(checksum, "checksum")
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
                self._run_repository_write(
                    lambda repository: repository.acquisition_outputs.save(existing)
                )
            return existing
        output = AcquisitionOutput(
            output_id=uuid4(),
            session_id=session_id,
            file_path=cleaned_path,
            checksum=cleaned_checksum,
            size_bytes=size_bytes,
        )
        self._store.acquisition_outputs[output.output_id] = output
        self._run_repository_write(lambda repository: repository.acquisition_outputs.save(output))
        return output

    def list_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
    ) -> list[AcquisitionOutput]:
        repository = self._active_repository()
        if repository is not None and not self._allow_in_memory:
            outputs, _ = repository.query_acquisition_outputs(
                session_id=session_id,
                limit=None,
                offset=0,
            )
            return self._cache_entities(
                "acquisition_outputs",
                outputs,
                lambda output: output.output_id,
            )
        outputs = list(self._store.acquisition_outputs.values())
        if session_id is None:
            return outputs
        return [output for output in outputs if output.session_id == session_id]

    def delete_acquisition_output(
        self, output_id: UUID, *, actor: AuthContext | None = None
    ) -> AcquisitionOutput:
        require_role(actor, WRITE_ROLES)
        output = self._get_from_repository_or_store(
            attribute_name="acquisition_outputs",
            entity_id=output_id,
            label="Acquisition output",
            loader=lambda repository: repository.acquisition_outputs.get(output_id),
        )
        self._store.acquisition_outputs.pop(output_id, None)
        self._run_repository_write(
            lambda repository: repository.acquisition_outputs.delete(output_id)
        )
        return output

    def promote_operational_session(
        self,
        session_id: UUID,
        primary_question_id: UUID,
        *,
        actor: AuthContext | None = None,
    ) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError(
                "Only operational sessions can be promoted to scientific sessions."
            )
        if session.status != SessionStatus.ACTIVE:
            raise ValidationError("Only active operational sessions can be promoted.")
        question = self.get_question(primary_question_id)
        if question.project_id != session.project_id:
            raise ValidationError("Primary question must belong to the same project.")
        if question.status != QuestionStatus.ACTIVE:
            raise ValidationError("Primary question must be active for scientific sessions.")
        session.session_type = SessionType.SCIENTIFIC
        session.primary_question_id = primary_question_id
        session.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.sessions.save(session))
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
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
        if session.status != SessionStatus.ACTIVE:
            raise ValidationError("Only active operational sessions can be promoted.")
        outputs = self.list_acquisition_outputs(session_id=session.session_id)
        merged_manifest = _merge_acquisition_outputs(commit_manifest, outputs)
        manifest_with_session = _manifest_input_with_source(merged_manifest, session.session_id)
        return self.create_dataset(
            project_id=session.project_id,
            primary_question_id=primary_question_id,
            secondary_question_ids=secondary_question_ids,
            status=status,
            commit_manifest=manifest_with_session,
            actor=actor,
        )

    def _ensure_source_session_valid(
        self, source_session_id: UUID | None, project_id: UUID
    ) -> None:
        if source_session_id is None:
            return
        session = self.get_session(source_session_id)
        if session.project_id != project_id:
            raise ValidationError("Source session must belong to the same project.")
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
