from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.acquisition_watcher import AcquisitionOutputWatcher
from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
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


def test_register_acquisition_output_accepts_scientific_session():
    api = repository_backed_api()
    actor = _actor()
    project = api.create_project("Neuro Project", actor=actor)
    question = api.create_question(
        project_id=project.project_id,
        text="Is the rig stable?",
        question_type=QuestionType.DESCRIPTIVE,
        status=QuestionStatus.ACTIVE,
        actor=actor,
    )
    session = api.create_session(
        project_id=project.project_id,
        session_type=SessionType.SCIENTIFIC,
        primary_question_id=question.question_id,
        actor=actor,
    )
    output = api.register_acquisition_output(
        session.session_id,
        file_path="output.bin",
        checksum="abc123",
        actor=actor,
    )
    assert output.session_id == session.session_id


def test_promote_operational_session_merges_outputs():
    api = repository_backed_api()
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
        size_bytes=4096,
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
    dataset = api.promote_operational_session_to_dataset(
        session.session_id,
        primary_question_id=question.question_id,
        commit_manifest=manifest,
        actor=actor,
    )
    files = {file.path: file for file in dataset.commit_manifest.files}
    assert files["acq.bin"].size_bytes == 4096
    assert files["rig.log"].size_bytes is None


def test_register_acquisition_output_updates_existing():
    api = repository_backed_api()
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
    api = repository_backed_api()
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


def test_acquisition_output_watcher_skips_hash_for_unchanged_files(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = repository_backed_api()
    actor = _actor()
    _, session = _operational_session(api, actor)
    output_path = tmp_path / "output.bin"
    output_path.write_text("hello")
    hash_calls: list[str] = []

    def counting_hash(path):
        hash_calls.append(path.name)
        return "sha256:counted"

    monkeypatch.setattr("lab_tracker.acquisition_watcher._hash_file", counting_hash)
    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [tmp_path],
        actor=actor,
        base_path=tmp_path,
    )

    assert len(watcher.scan()) == 1
    assert watcher.scan() == []
    assert hash_calls == ["output.bin"]


def test_acquisition_output_watcher_hidden_checks_are_relative(tmp_path):
    api = repository_backed_api()
    actor = _actor()
    _, session = _operational_session(api, actor)
    watch_root = tmp_path / ".outer" / "watch"
    watch_root.mkdir(parents=True)
    output_path = watch_root / "output.bin"
    output_path.write_text("hello")
    hidden_dir = watch_root / ".partial"
    hidden_dir.mkdir()
    (hidden_dir / "skip.bin").write_text("skip")

    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [watch_root],
        actor=actor,
        base_path=watch_root,
    )

    outputs = watcher.scan()

    assert [output.file_path for output in outputs] == ["output.bin"]


def test_acquisition_output_watcher_waits_for_stable_file(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    api = repository_backed_api()
    actor = _actor()
    _, session = _operational_session(api, actor)
    output_path = tmp_path / "output.bin"
    output_path.write_text("first")

    def changing_hash(path):
        path.write_text("second")
        return "first-hash"

    monkeypatch.setattr("lab_tracker.acquisition_watcher._hash_file", changing_hash)
    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [tmp_path],
        actor=actor,
        base_path=tmp_path,
    )

    assert watcher.scan() == []
    assert api.list_acquisition_outputs(session_id=session.session_id) == []


def test_acquisition_output_watcher_continues_after_register_error(tmp_path, monkeypatch):
    api = repository_backed_api()
    actor = _actor()
    _, session = _operational_session(api, actor)
    bad_path = tmp_path / "bad.bin"
    good_path = tmp_path / "good.bin"
    bad_path.write_text("bad")
    good_path.write_text("good")
    original_register = api.register_acquisition_output
    calls: list[str] = []

    def flaky_register(session_id, *, file_path, **kwargs):
        calls.append(file_path)
        if file_path == "bad.bin":
            raise RuntimeError("database unavailable")
        return original_register(session_id, file_path=file_path, **kwargs)

    monkeypatch.setattr(api, "register_acquisition_output", flaky_register)
    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [tmp_path],
        actor=actor,
        base_path=tmp_path,
        failure_backoff_seconds=60,
    )

    outputs = watcher.scan()
    assert [output.file_path for output in outputs] == ["good.bin"]

    assert watcher.scan() == []
    assert calls.count("bad.bin") == 1
