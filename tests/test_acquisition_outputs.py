from uuid import uuid4

import pytest

from lab_tracker.acquisition_watcher import AcquisitionOutputWatcher
from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    DatasetCommitManifestInput,
    DatasetFile,
    QuestionStatus,
    QuestionType,
    SessionType,
)


def _actor(role: Role = Role.ADMIN) -> AuthContext:
    return AuthContext(user_id=uuid4(), role=role)


def _operational_session(api: LabTrackerAPI, actor: AuthContext):
    project = api.create_project("Rig Project", actor=actor)
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.OPERATIONAL,
        actor=actor,
    )
    return project, session


def test_register_acquisition_output_requires_operational_session():
    api = LabTrackerAPI()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the rig stable?",
        question_type=QuestionType.DESCRIPTIVE,
        actor=actor,
    )
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.SCIENTIFIC,
        primary_question_id=question.question_id,
        actor=actor,
    )
    with pytest.raises(ValidationError):
        api.register_acquisition_output(
            session.session_id,
            file_path="output.bin",
            checksum="abc123",
            actor=actor,
        )


def test_promote_operational_session_merges_outputs():
    api = LabTrackerAPI()
    actor = _actor()
    project, session = _operational_session(api, actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Did the rig pass QA?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    api.register_acquisition_output(
        session.session_id,
        file_path="acq.bin",
        checksum="abc123",
        actor=actor,
    )
    api.register_acquisition_output(
        session.session_id,
        file_path="rig.log",
        checksum="def456",
        actor=actor,
    )
    manifest = DatasetCommitManifestInput(
        files=[DatasetFile(path="rig.log", checksum="def456")],
        metadata={"run": "7"},
    )
    dataset = api.promote_operational_session(
        session.session_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    paths = {file.path for file in dataset.commit_manifest.files}
    assert "acq.bin" in paths
    assert "rig.log" in paths


def test_register_acquisition_output_updates_existing():
    api = LabTrackerAPI()
    actor = _actor()
    _, session = _operational_session(api, actor)
    output = api.register_acquisition_output(
        session.session_id,
        file_path="acq.bin",
        checksum="abc123",
        actor=actor,
    )
    updated = api.register_acquisition_output(
        session.session_id,
        file_path="acq.bin",
        checksum="def456",
        actor=actor,
    )
    assert output.output_id == updated.output_id
    assert updated.checksum == "def456"


def test_acquisition_output_watcher_registers_outputs(tmp_path):
    api = LabTrackerAPI()
    actor = _actor()
    _, session = _operational_session(api, actor)
    output_path = tmp_path / "output.bin"
    output_path.write_text("hello")
    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [tmp_path],
        actor=actor,
        base_path=tmp_path,
    )
    outputs = watcher.scan()
    assert len(outputs) == 1
    assert outputs[0].file_path == "output.bin"
    assert watcher.scan() == []
