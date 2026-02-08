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
    Session,
    SessionStatus,
    SessionType,
    decode_session_link_code,
    utc_now,
)
from lab_tracker.services.shared import (
    WRITE_ROLES,
    _ensure_non_empty,
    _find_acquisition_output,
    _get_or_raise,
    _manifest_input_with_source,
    _merge_acquisition_outputs,
)


class SessionServiceMixin:
    def create_session(
        self,
        project_id: UUID,
        session_type: SessionType,
        *,
        primary_question_id: UUID | None = None,
        status: SessionStatus = SessionStatus.ACTIVE,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Session:
        require_role(actor, WRITE_ROLES)
        self.get_project(project_id)
        if session_type == SessionType.SCIENTIFIC and primary_question_id is None:
            raise ValidationError("Scientific sessions require a primary question.")
        if primary_question_id is not None:
            question = self.get_question(primary_question_id)
            if question.project_id != project_id:
                raise ValidationError("Primary question must belong to the same project.")
        session = Session(
            session_id=uuid4(),
            project_id=project_id,
            session_type=session_type,
            status=status,
            primary_question_id=primary_question_id,
            created_by=created_by,
        )
        self._store.sessions[session.session_id] = session
        self._run_repository_write(lambda repository: repository.sessions.save(session))
        return session

    def get_session(self, session_id: UUID) -> Session:
        return _get_or_raise(self._store.sessions, session_id, "Session")

    def get_session_by_link_code(self, link_code: str) -> Session:
        _ensure_non_empty(link_code, "link_code")
        try:
            session_id = decode_session_link_code(link_code)
        except ValueError as exc:
            raise ValidationError("Invalid session link code.") from exc
        return self.get_session(session_id)

    def list_sessions(self, *, project_id: UUID | None = None) -> list[Session]:
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
        if status is not None:
            session.status = status
        if ended_at is not None:
            session.ended_at = ended_at
        session.updated_at = utc_now()
        self._run_repository_write(lambda repository: repository.sessions.save(session))
        return session

    def delete_session(self, session_id: UUID, *, actor: AuthContext | None = None) -> Session:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        del self._store.sessions[session_id]
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
        session = self.get_session(session_id)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Acquisition outputs require an operational session.")
        _ensure_non_empty(file_path, "file_path")
        _ensure_non_empty(checksum, "checksum")
        if size_bytes is not None and size_bytes < 0:
            raise ValidationError("size_bytes must be 0 or greater.")
        cleaned_path = file_path.strip()
        cleaned_checksum = checksum.strip()
        existing = _find_acquisition_output(
            self._store.acquisition_outputs, session_id, cleaned_path
        )
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
                try:
                    self._run_repository_write(
                        lambda repository: repository.acquisition_outputs.save(existing)
                    )
                except NotImplementedError:
                    pass
            return existing
        output = AcquisitionOutput(
            output_id=uuid4(),
            session_id=session_id,
            file_path=cleaned_path,
            checksum=cleaned_checksum,
            size_bytes=size_bytes,
        )
        self._store.acquisition_outputs[output.output_id] = output
        try:
            self._run_repository_write(
                lambda repository: repository.acquisition_outputs.save(output)
            )
        except NotImplementedError:
            pass
        return output

    def list_acquisition_outputs(
        self,
        *,
        session_id: UUID | None = None,
    ) -> list[AcquisitionOutput]:
        outputs = list(self._store.acquisition_outputs.values())
        if session_id is None:
            return outputs
        return [output for output in outputs if output.session_id == session_id]

    def delete_acquisition_output(
        self, output_id: UUID, *, actor: AuthContext | None = None
    ) -> AcquisitionOutput:
        require_role(actor, WRITE_ROLES)
        output = _get_or_raise(
            self._store.acquisition_outputs,
            output_id,
            "Acquisition output",
        )
        del self._store.acquisition_outputs[output_id]
        try:
            self._run_repository_write(
                lambda repository: repository.acquisition_outputs.delete(output_id)
            )
        except NotImplementedError:
            pass
        return output

    def promote_operational_session(
        self,
        session_id: UUID,
        primary_question_id: UUID,
        *,
        secondary_question_ids: Iterable[UUID] | None = None,
        status: DatasetStatus = DatasetStatus.COMMITTED,
        commit_manifest: DatasetCommitManifestInput | DatasetCommitManifest | None = None,
        actor: AuthContext | None = None,
        created_by: str | None = None,
    ) -> Dataset:
        require_role(actor, WRITE_ROLES)
        session = self.get_session(session_id)
        if session.session_type != SessionType.OPERATIONAL:
            raise ValidationError("Only operational sessions can be promoted to datasets.")
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
            created_by=created_by,
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
