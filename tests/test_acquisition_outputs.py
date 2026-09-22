import logging
import os
import threading
from uuid import uuid4

import pytest
from api_helpers import repository_backed_api

from lab_tracker.acquisition_watcher import AcquisitionOutputWatcher
from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.errors import AuthError, NotFoundError, RateLimitError, ValidationError
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


def _watcher_with_register(tmp_path, register, **kwargs):
    api = repository_backed_api()
    actor = _actor()
    _, session = _operational_session(api, actor)
    api.register_acquisition_output = register  # type: ignore[method-assign]
    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [tmp_path],
        actor=actor,
        base_path=tmp_path,
        **kwargs,
    )
    return watcher, session


def test_acquisition_output_watcher_logs_transient_failures_with_context(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    (tmp_path / "bad.bin").write_text("bad")

    def failing_register(session_id, *, file_path, **kwargs):
        raise RuntimeError("database unavailable")

    watcher, session = _watcher_with_register(
        tmp_path, failing_register, failure_backoff_seconds=0
    )

    with caplog.at_level(logging.WARNING, logger="lab_tracker.acquisition_watcher"):
        assert watcher.scan() == []

    records = [r for r in caplog.records if r.name == "lab_tracker.acquisition_watcher"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "bad.bin" in message
    assert str(session.session_id) in message
    assert "database unavailable" in message
    assert record.exc_info is not None
    failure = watcher.failures[tmp_path / "bad.bin"]
    assert failure.attempts == 1
    assert "database unavailable" in failure.last_error
    assert watcher.failure_count == 1


def test_acquisition_output_watcher_backs_off_exponentially(tmp_path, monkeypatch):
    (tmp_path / "bad.bin").write_text("bad")
    clock = [1000.0]
    monkeypatch.setattr("lab_tracker.acquisition_watcher.time.monotonic", lambda: clock[0])
    calls: list[str] = []

    def failing_register(session_id, *, file_path, **kwargs):
        calls.append(file_path)
        raise RuntimeError("database unavailable")

    watcher, _ = _watcher_with_register(
        tmp_path,
        failing_register,
        failure_backoff_seconds=5,
        max_failure_backoff_seconds=12,
    )

    watcher.scan()  # attempt 1 -> retry after 5s
    clock[0] += 4.9
    watcher.scan()
    assert len(calls) == 1
    clock[0] += 0.2
    watcher.scan()  # attempt 2 -> retry after 10s
    assert len(calls) == 2
    clock[0] += 9.9
    watcher.scan()
    assert len(calls) == 2
    clock[0] += 0.2
    watcher.scan()  # attempt 3 -> capped at 12s
    assert len(calls) == 3
    assert watcher.failures[tmp_path / "bad.bin"].retry_after == pytest.approx(clock[0] + 12)


def test_acquisition_output_watcher_accepts_base_backoff_above_default_max(tmp_path, monkeypatch):
    # Callers written before max_failure_backoff_seconds existed may pass a
    # base backoff above its 300s default; the default cap must follow the base.
    (tmp_path / "bad.bin").write_text("bad")
    clock = [1000.0]
    monkeypatch.setattr("lab_tracker.acquisition_watcher.time.monotonic", lambda: clock[0])

    def failing_register(session_id, *, file_path, **kwargs):
        raise RuntimeError("database unavailable")

    watcher, _ = _watcher_with_register(tmp_path, failing_register, failure_backoff_seconds=600)

    watcher.scan()  # attempt 1 -> retry after the 600s base
    assert watcher.failures[tmp_path / "bad.bin"].retry_after == pytest.approx(clock[0] + 600)
    clock[0] += 600.1
    watcher.scan()  # attempt 2 -> capped at the base, never below it
    assert watcher.failures[tmp_path / "bad.bin"].attempts == 2
    assert watcher.failures[tmp_path / "bad.bin"].retry_after == pytest.approx(clock[0] + 600)


def test_acquisition_output_watcher_keeps_300s_default_cap_for_small_base(tmp_path, monkeypatch):
    (tmp_path / "bad.bin").write_text("bad")
    clock = [1000.0]
    monkeypatch.setattr("lab_tracker.acquisition_watcher.time.monotonic", lambda: clock[0])

    def failing_register(session_id, *, file_path, **kwargs):
        raise RuntimeError("database unavailable")

    watcher, _ = _watcher_with_register(tmp_path, failing_register, failure_backoff_seconds=200)

    watcher.scan()  # attempt 1 -> 200s
    clock[0] += 200.1
    watcher.scan()  # attempt 2 -> 400s capped at the 300s default
    assert watcher.failures[tmp_path / "bad.bin"].retry_after == pytest.approx(clock[0] + 300)


def test_acquisition_output_watcher_rejects_explicit_max_below_base(tmp_path):
    with pytest.raises(ValueError, match="max_failure_backoff_seconds"):
        AcquisitionOutputWatcher(
            repository_backed_api(),
            uuid4(),
            [tmp_path],
            failure_backoff_seconds=600,
            max_failure_backoff_seconds=300,
        )


def test_acquisition_output_watcher_escalates_persistent_failures(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    (tmp_path / "bad.bin").write_text("bad")

    def failing_register(session_id, *, file_path, **kwargs):
        raise RuntimeError("database unavailable")

    watcher, _ = _watcher_with_register(
        tmp_path,
        failing_register,
        failure_backoff_seconds=0,
        persistent_failure_threshold=3,
    )

    with caplog.at_level(logging.WARNING, logger="lab_tracker.acquisition_watcher"):
        for _ in range(3):
            watcher.scan()

    levels = [r.levelno for r in caplog.records if r.name == "lab_tracker.acquisition_watcher"]
    assert levels == [logging.WARNING, logging.WARNING, logging.ERROR]
    assert watcher.failures[tmp_path / "bad.bin"].attempts == 3


def test_acquisition_output_watcher_clears_failure_after_success(tmp_path):
    (tmp_path / "flaky.bin").write_text("data")
    api = repository_backed_api()
    actor = _actor()
    _, session = _operational_session(api, actor)
    original_register = api.register_acquisition_output
    attempts = {"count": 0}

    def flaky_register(session_id, *, file_path, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("database unavailable")
        return original_register(session_id, file_path=file_path, **kwargs)

    api.register_acquisition_output = flaky_register  # type: ignore[method-assign]
    watcher = AcquisitionOutputWatcher(
        api,
        session.session_id,
        [tmp_path],
        actor=actor,
        base_path=tmp_path,
        failure_backoff_seconds=0,
    )

    assert watcher.scan() == []
    assert [output.file_path for output in watcher.scan()] == ["flaky.bin"]
    assert watcher.failures == {}
    assert watcher.failure_count == 1


@pytest.mark.parametrize(
    "error",
    [NotFoundError("Session does not exist."), AuthError("Contributor role required.")],
)
def test_acquisition_output_watcher_raises_on_session_level_errors(
    tmp_path, caplog: pytest.LogCaptureFixture, error
):
    (tmp_path / "output.bin").write_text("data")

    def failing_register(session_id, *, file_path, **kwargs):
        raise error

    watcher, session = _watcher_with_register(tmp_path, failing_register)

    with caplog.at_level(logging.ERROR, logger="lab_tracker.acquisition_watcher"):
        with pytest.raises(type(error)):
            watcher.scan()
        with pytest.raises(type(error)):
            watcher.run(interval=0.01, stop_event=threading.Event())

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert messages and str(session.session_id) in messages[0]


def test_acquisition_output_watcher_rate_limit_is_transient(tmp_path):
    (tmp_path / "output.bin").write_text("data")

    def failing_register(session_id, *, file_path, **kwargs):
        raise RateLimitError("Too many attempts.")

    watcher, _ = _watcher_with_register(tmp_path, failing_register)

    assert watcher.scan() == []
    assert watcher.failures[tmp_path / "output.bin"].attempts == 1


def test_acquisition_output_watcher_does_not_retry_rejected_file_until_it_changes(
    tmp_path, caplog: pytest.LogCaptureFixture
):
    output_path = tmp_path / "output.bin"
    output_path.write_text("data")
    calls: list[str] = []

    def rejecting_register(session_id, *, file_path, **kwargs):
        calls.append(file_path)
        raise ValidationError("file_path must not be empty.")

    watcher, _ = _watcher_with_register(
        tmp_path, rejecting_register, failure_backoff_seconds=0
    )

    with caplog.at_level(logging.ERROR, logger="lab_tracker.acquisition_watcher"):
        assert watcher.scan() == []
        assert watcher.scan() == []
    assert calls == ["output.bin"]
    assert any(
        r.levelno == logging.ERROR and "output.bin" in r.getMessage() for r in caplog.records
    )
    assert watcher.failures[output_path].attempts == 1

    output_path.write_text("changed data")
    os.utime(output_path, (1, 1))
    watcher.scan()
    assert calls == ["output.bin", "output.bin"]


def test_acquisition_output_watcher_forgets_failures_for_deleted_files(tmp_path):
    output_path = tmp_path / "bad.bin"
    output_path.write_text("bad")

    def failing_register(session_id, *, file_path, **kwargs):
        raise RuntimeError("database unavailable")

    watcher, _ = _watcher_with_register(tmp_path, failing_register, failure_backoff_seconds=0)

    watcher.scan()
    assert output_path in watcher.failures
    output_path.unlink()
    watcher.scan()
    assert watcher.failures == {}
