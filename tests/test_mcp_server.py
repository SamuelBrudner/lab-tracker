from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine

import lab_tracker.db_models  # noqa: F401
from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import AuthContext, Role
from lab_tracker.config import Settings
from lab_tracker.db import Base
from lab_tracker.mcp_server import LabTrackerMCPRuntime, register_lab_tracker_mcp_interface


class FakeMCP:
    def __init__(self) -> None:
        self.tools = {}
        self.resources = {}
        self.prompts = {}

    def tool(self, *args, **kwargs):  # noqa: ANN001, ANN202, ARG002
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator

    def resource(self, uri, *args, **kwargs):  # noqa: ANN001, ANN202, ARG002
        def decorator(func):
            self.resources[uri] = func
            return func

        return decorator

    def prompt(self, *args, **kwargs):  # noqa: ANN001, ANN202, ARG002
        def decorator(func):
            self.prompts[func.__name__] = func
            return func

        return decorator


class InMemoryRuntime:
    def __init__(self) -> None:
        self.api = LabTrackerAPI.in_memory()
        self.actor = AuthContext(user_id=uuid4(), role=Role.ADMIN)

    def execute(self, operation):
        return operation(self.api)


def test_mcp_tools_cover_lab_record_round_trip():
    mcp = FakeMCP()
    register_lab_tracker_mcp_interface(mcp, InMemoryRuntime())

    project = mcp.tools["lab_tracker_create_project"](
        name="LLM Lab",
        description="MCP-facing project",
    )
    question = mcp.tools["lab_tracker_create_question"](
        project_id=project["project_id"],
        text="Does the rig stay stable during baseline recordings?",
        question_type="descriptive",
        status="active",
    )
    note = mcp.tools["lab_tracker_record_note"](
        project_id=project["project_id"],
        raw_content="Baseline looked stable after warmup.",
        targets=[
            {
                "entity_type": "question",
                "entity_id": question["question_id"],
            }
        ],
        metadata={"source": "bench"},
    )

    search = mcp.tools["lab_tracker_search"]("warmup", project_id=project["project_id"])
    overview = mcp.tools["lab_tracker_overview"](project_id=project["project_id"])
    context = mcp.tools["lab_tracker_get_project_context"](project["project_id"])
    resource = json.loads(mcp.resources["lab-tracker://project/{project_id}"](project["project_id"]))
    prompt = mcp.prompts["lab_tracker_workflow_prompt"]("capture baseline work")

    assert question["status"] == "active"
    assert note["targets"][0]["entity_id"] == question["question_id"]
    assert search["counts"] == {"questions": 0, "notes": 1}
    assert overview["counts"]["questions"] == 1
    assert context["counts"]["notes"] == 1
    assert resource["project"]["project_id"] == project["project_id"]
    assert "Do not invent checksums" in prompt


def test_mcp_runtime_persists_with_sqlalchemy(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'mcp.db'}"
    settings = Settings(
        database_url=database_url,
        note_storage_path=str(tmp_path / "notes"),
        file_storage_path=str(tmp_path / "files"),
        auth_secret_key="test-secret",
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
