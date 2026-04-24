from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine

import lab_tracker.db_models  # noqa: F401
from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings
from lab_tracker.db import Base
from lab_tracker.mcp_server import (
    REVIEW_DASHBOARD_MIME_TYPE,
    REVIEW_DASHBOARD_URI,
    LabTrackerMCPRuntime,
    build_mcp_server,
    register_lab_tracker_mcp_interface,
)
from lab_tracker.models import QuestionType


class FakeMCP:
    def __init__(self) -> None:
        self.tools = {}
        self.tool_meta = {}
        self.resources = {}
        self.resource_meta = {}
        self.prompts = {}

    def tool(self, name=None, **kwargs):  # noqa: ANN001, ANN202
        def decorator(func):
            tool_name = name or func.__name__
            self.tools[tool_name] = func
            self.tool_meta[tool_name] = kwargs
            return func

        return decorator

    def resource(self, uri, **kwargs):  # noqa: ANN001, ANN202
        def decorator(func):
            self.resources[uri] = func
            self.resource_meta[uri] = kwargs
            return func

        return decorator

    def prompt(self, name=None, **kwargs):  # noqa: ANN001, ANN202, ARG002
        def decorator(func):
            self.prompts[name or func.__name__] = func
            return func

        return decorator


class InMemoryRuntime:
    def __init__(
        self,
        *,
        role: Role = Role.VIEWER,
        enable_writes: bool = False,
        expose_legacy_tools: bool = False,
    ) -> None:
        self.api = LabTrackerAPI.in_memory()
        self.actor = AuthContext(user_id=uuid4(), role=role)
        self.enable_writes = enable_writes
        self.expose_legacy_tools = expose_legacy_tools

    def execute(self, operation):
        return operation(self.api)

    def seed_project(self, name: str = "LLM Lab"):
        return self.api.create_project(
            name,
            actor=AuthContext(user_id=uuid4(), role=Role.ADMIN),
        )


READ_TOOL_NAMES = {"lab_context", "search_lab_context", "refresh_review_dashboard"}
WRITE_TOOL_NAMES = {
    "capture_note",
    "stage_question",
    "update_staged_question",
    "activate_question",
    "start_session",
    "close_session",
}


def test_curated_tools_are_public_by_default_and_legacy_tools_are_hidden():
    mcp = FakeMCP()
    register_lab_tracker_mcp_interface(mcp, InMemoryRuntime())

    assert set(mcp.tools) == READ_TOOL_NAMES
    assert "lab_tracker_create_project" not in mcp.tools
    assert REVIEW_DASHBOARD_URI in mcp.resources
    assert mcp.resource_meta[REVIEW_DASHBOARD_URI]["mime_type"] == REVIEW_DASHBOARD_MIME_TYPE


def test_legacy_tools_are_debug_only():
    mcp = FakeMCP()
    register_lab_tracker_mcp_interface(
        mcp,
        InMemoryRuntime(expose_legacy_tools=True),
    )

    assert READ_TOOL_NAMES.issubset(mcp.tools)
    assert "lab_tracker_create_project" in mcp.tools


def test_tool_descriptors_include_app_metadata_and_annotations():
    mcp = FakeMCP()
    register_lab_tracker_mcp_interface(
        mcp,
        InMemoryRuntime(role=Role.EDITOR, enable_writes=True),
    )

    context_meta = mcp.tool_meta["lab_context"]
    assert context_meta["title"] == "Show lab context"
    assert context_meta["annotations"].readOnlyHint is True
    assert context_meta["meta"]["ui"]["resourceUri"] == REVIEW_DASHBOARD_URI
    assert context_meta["meta"]["openai/outputTemplate"] == REVIEW_DASHBOARD_URI

    write_meta = mcp.tool_meta["capture_note"]
    assert write_meta["annotations"].readOnlyHint is False
    assert write_meta["annotations"].destructiveHint is False
    assert write_meta["annotations"].openWorldHint is False


def test_write_tools_require_explicit_enable_and_editor_role():
    default_mcp = FakeMCP()
    register_lab_tracker_mcp_interface(default_mcp, InMemoryRuntime())
    assert WRITE_TOOL_NAMES.isdisjoint(default_mcp.tools)

    viewer_mcp = FakeMCP()
    register_lab_tracker_mcp_interface(
        viewer_mcp,
        InMemoryRuntime(role=Role.VIEWER, enable_writes=True),
    )
    assert WRITE_TOOL_NAMES.isdisjoint(viewer_mcp.tools)

    editor_mcp = FakeMCP()
    register_lab_tracker_mcp_interface(
        editor_mcp,
        InMemoryRuntime(role=Role.EDITOR, enable_writes=True),
    )
    assert WRITE_TOOL_NAMES.issubset(editor_mcp.tools)


def test_chatgpt_capture_review_workflow_round_trip():
    runtime = InMemoryRuntime(role=Role.EDITOR, enable_writes=True)
    project = runtime.seed_project()
    mcp = FakeMCP()
    register_lab_tracker_mcp_interface(mcp, runtime)

    staged = mcp.tools["stage_question"](
        project_id=str(project.project_id),
        text="Does the rig stay stable during baseline recordings?",
        question_type=QuestionType.DESCRIPTIVE.value,
    )
    question_id = staged.structuredContent["question"]["question_id"]

    updated = mcp.tools["update_staged_question"](
        question_id=question_id,
        hypothesis="The baseline remains stable after warmup.",
    )
    assert updated.structuredContent["question"]["hypothesis"] == (
        "The baseline remains stable after warmup."
    )

    note = mcp.tools["capture_note"](
        project_id=str(project.project_id),
        raw_content="Baseline looked stable after warmup.",
        target_entity_type="question",
        target_entity_id=question_id,
    )
    assert note.structuredContent["note"]["metadata"]["created_via"] == "chatgpt_app"

    active = mcp.tools["activate_question"](question_id=question_id)
    session = mcp.tools["start_session"](
        project_id=str(project.project_id),
        session_type="scientific",
        primary_question_id=active.structuredContent["question"]["question_id"],
    )
    closed = mcp.tools["close_session"](
        session_id=session.structuredContent["session"]["session_id"],
    )
    refreshed = mcp.tools["refresh_review_dashboard"](project_id=str(project.project_id))
    search = mcp.tools["search_lab_context"]("warmup", project_id=str(project.project_id))

    assert closed.structuredContent["session"]["status"] == "closed"
    assert refreshed.structuredContent["dashboard"]["counts"]["recent_notes"] == 1
    assert search.structuredContent["counts"] == {"questions": 1, "notes": 1}
    assert refreshed.meta["dashboard"]["project"]["project_id"] == str(project.project_id)


def test_review_dashboard_widget_is_packaged_and_host_bridge_only():
    mcp = FakeMCP()
    register_lab_tracker_mcp_interface(mcp, InMemoryRuntime())

    html = mcp.resources[REVIEW_DASHBOARD_URI]()

    assert "Lab Tracker Review" in html
    assert "ui/notifications/tool-result" in html
    assert "tools/call" in html
    assert "fetch(" not in html
    assert "Authorization" not in html


def test_mcp_runtime_persists_with_sqlalchemy(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'mcp.db'}"
    settings = Settings(
        database_url=database_url,
        note_storage_path=str(tmp_path / "notes"),
        file_storage_path=str(tmp_path / "files"),
        auth_secret_key="test-secret",
        mcp_actor_role="admin",
        mcp_enable_writes=True,
    )
    engine = create_engine(
        database_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    runtime = LabTrackerMCPRuntime(settings=settings, engine=engine)

    try:
        project = runtime.execute(
            lambda api: api.create_project(
                name="Persisted",
                actor=runtime.actor,
            )
        )
        projects = runtime.execute(lambda api: api.list_projects())
    finally:
        runtime.close()
        engine.dispose()

    assert [project.name for project in projects] == [project.name]


def test_build_mcp_server_uses_configurable_http_bind():
    server = build_mcp_server(runtime=InMemoryRuntime(), host="0.0.0.0", port=8123)

    assert server.settings.host == "0.0.0.0"
    assert server.settings.port == 8123
    assert server.settings.streamable_http_path == "/mcp"
