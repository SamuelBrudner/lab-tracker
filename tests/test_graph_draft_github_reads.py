from __future__ import annotations

import base64
from uuid import UUID

import httpx
import pytest

from lab_tracker.auth import Role
from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import DataStore, StoreKind
from lab_tracker.services.graph_draft_github_reads import (
    GitHubRepositoryReader,
    parse_github_repository,
)
from lab_tracker.services.graph_draft_harness_mcp import HarnessGraphDraftMCPServer
from lab_tracker.services.graph_draft_read_tools import (
    GITHUB_REPOSITORY_READ_TOOL_ALLOWLIST,
    ScopedGraphDraftReadToolExecutor,
)
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository

_COMMIT = "a" * 40


def _create_project(client, headers, name: str, *, group_id: str | None = None) -> str:
    payload = {"name": name}
    if group_id is not None:
        payload["group_id"] = group_id
    response = client.post("/projects", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _create_group(client, headers, name: str) -> str:
    response = client.post("/groups", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["group_id"]


def _create_store(
    client,
    headers,
    *,
    name: str,
    root: str,
    project_id: str | None = None,
    group_id: str | None = None,
) -> str:
    payload = {
        "project_id": project_id,
        "group_id": group_id,
        "name": name,
        "kind": "git",
        "root": root,
        "is_default": False,
    }
    response = client.post("/data-stores", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["store_id"]


def _viewer_for_project(client, headers, project_id: str) -> str:
    user = client.app.state.auth_service.register_user(
        username=f"github-reader-{project_id[:8]}",
        password="secret",
        role=Role.VIEWER,
    )
    response = client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(user.user_id), "role": "viewer"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(user.user_id)


def _github_transport(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer read-only-token"
    assert request.url.params["ref"] == _COMMIT
    if request.url.path.endswith("/contents/src"):
        return httpx.Response(
            200,
            json=[
                {"path": "src/model.py", "type": "file", "sha": "blob-1", "size": 27},
                {"path": "src/util.py", "type": "file", "sha": "blob-2", "size": 12},
            ],
        )
    if request.url.path.endswith("/contents/src/model.py"):
        content = b"def model():\n    return 42\n"
        return httpx.Response(
            200,
            json={
                "path": "src/model.py",
                "type": "file",
                "sha": "blob-1",
                "size": len(content),
                "encoding": "base64",
                "content": base64.b64encode(content).decode("ascii"),
            },
        )
    return httpx.Response(404, json={"message": "not found"})


def test_github_reader_accepts_supported_remotes_and_requires_pinned_safe_paths() -> None:
    assert parse_github_repository("https://github.com/acme/analysis.git").slug == (
        "acme/analysis"
    )
    assert parse_github_repository("git@github.com:acme/analysis.git").slug == (
        "acme/analysis"
    )
    assert parse_github_repository("https://gitlab.com/acme/analysis.git") is None

    reader = GitHubRepositoryReader(
        token="read-only-token",
        transport=httpx.MockTransport(_github_transport),
        max_file_bytes=1024,
    )
    store = DataStore(
        store_id=UUID("00000000-0000-4000-8000-000000000010"),
        project_id=UUID("00000000-0000-4000-8000-000000000011"),
        name="analysis",
        kind=StoreKind.GIT,
        root="https://github.com/acme/analysis.git",
    )

    listing = reader.list_files(store, commit=_COMMIT, path="src", limit=1)
    assert listing["items"] == [
        {"path": "src/model.py", "type": "file", "sha": "blob-1", "size_bytes": 27}
    ]
    assert listing["truncated"] is True
    content = reader.read_file(store, commit=_COMMIT, path="src/model.py", max_bytes=10)
    assert content["content"] == "def model("
    assert content["truncated"] is True

    with pytest.raises(GraphDraftingError, match="full 40- or 64-character"):
        reader.read_file(store, commit="main", path="src/model.py")
    with pytest.raises(GraphDraftingError, match="stay within"):
        reader.read_file(store, commit=_COMMIT, path="../secret")


def test_github_reader_resolves_named_gh_cli_credential_without_exposing_it() -> None:
    requested_refs: list[str] = []

    def resolve(credential_ref: str) -> str:
        requested_refs.append(credential_ref)
        return "read-only-token"

    reader = GitHubRepositoryReader(
        credential_resolver=resolve,
        transport=httpx.MockTransport(_github_transport),
        max_file_bytes=1024,
    )
    store = DataStore(
        store_id=UUID("00000000-0000-4000-8000-000000000012"),
        project_id=UUID("00000000-0000-4000-8000-000000000011"),
        name="analysis",
        kind=StoreKind.GIT,
        root="https://github.com/acme/analysis.git",
        credential_ref="gh-cli:github.com",
    )

    payload = reader.read_file(store, commit=_COMMIT, path="src/model.py")

    assert "return 42" in payload["content"]
    assert requested_refs == ["gh-cli:github.com"]
    assert "token" not in payload


def test_scoped_executor_exposes_only_project_relevant_github_stores(
    client,
    admin_auth_headers,
) -> None:
    group_id = _create_group(client, admin_auth_headers, "Repo readers")
    project_id = _create_project(
        client, admin_auth_headers, "Scoped repository project", group_id=group_id
    )
    foreign_project_id = _create_project(
        client, admin_auth_headers, "Foreign repository project"
    )
    own_store_id = _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="analysis",
        root="https://github.com/acme/analysis.git",
    )
    group_store_id = _create_store(
        client,
        admin_auth_headers,
        group_id=group_id,
        name="shared-code",
        root="git@github.com:acme/shared-code.git",
    )
    foreign_store_id = _create_store(
        client,
        admin_auth_headers,
        project_id=foreign_project_id,
        name="foreign",
        root="https://github.com/acme/foreign.git",
    )
    _create_store(
        client,
        admin_auth_headers,
        project_id=project_id,
        name="not-github",
        root="https://gitlab.com/acme/not-github.git",
    )
    viewer_user_id = _viewer_for_project(client, admin_auth_headers, project_id)
    reader = GitHubRepositoryReader(
        token="read-only-token",
        transport=httpx.MockTransport(_github_transport),
        max_file_bytes=1024,
    )

    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        executor = ScopedGraphDraftReadToolExecutor(
            repository=repository,
            authorization=api.project_authorization,
            project_id=UUID(project_id),
            target_user_id=UUID(viewer_user_id),
            sensitivity_policy="omit",
            goals=api.goals,
            publication_readiness=api.publication_readiness,
            github_reader=reader,
        )
        listed = executor.execute("list_relevant_repositories").payload
        assert {item["store_id"] for item in listed["data"]} == {
            own_store_id,
            group_store_id,
        }
        assert all("credential" not in item for item in listed["data"])

        file_payload = executor.execute(
            "read_repository_file",
            {"store_id": own_store_id, "commit": _COMMIT, "path": "src/model.py"},
        ).payload
        assert "return 42" in file_payload["data"]["content"]

        with pytest.raises(GraphDraftingError, match="not a GitHub repository"):
            executor.execute(
                "read_repository_file",
                {
                    "store_id": foreign_store_id,
                    "commit": _COMMIT,
                    "path": "src/model.py",
                },
            )

        harness = HarnessGraphDraftMCPServer(executor=executor, max_tool_calls=4)
        tool_names = {item["name"] for item in harness.tool_specs()}
        assert set(GITHUB_REPOSITORY_READ_TOOL_ALLOWLIST) <= tool_names
        assert "resolve_artifact" not in tool_names


def test_repository_tools_are_absent_when_github_reads_are_disabled(
    client,
    admin_auth_headers,
) -> None:
    project_id = _create_project(client, admin_auth_headers, "No repository reads")
    viewer_user_id = _viewer_for_project(client, admin_auth_headers, project_id)

    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        executor = ScopedGraphDraftReadToolExecutor(
            repository=repository,
            authorization=api.project_authorization,
            project_id=UUID(project_id),
            target_user_id=UUID(viewer_user_id),
            sensitivity_policy="omit",
            goals=api.goals,
            publication_readiness=api.publication_readiness,
        )
        tool_names = {item["name"] for item in executor.mcp_tool_specs()}
        assert not set(GITHUB_REPOSITORY_READ_TOOL_ALLOWLIST) & tool_names
        with pytest.raises(GraphDraftingError, match="not allowlisted"):
            executor.execute("list_relevant_repositories")


def test_graph_draft_service_injects_github_reader_only_when_enabled(
    client,
    admin_auth_headers,
) -> None:
    project_id = _create_project(client, admin_auth_headers, "Configured repository reads")
    viewer_user_id = _viewer_for_project(client, admin_auth_headers, project_id)

    class CapturingDraftClient:
        _tool_loop_enabled = True
        executor = None

        def configure_live_read_tools(self, executor) -> None:  # noqa: ANN001
            self.executor = executor

    draft_client = CapturingDraftClient()
    client.app.state.settings.graph_draft_github_read_enabled = True
    client.app.state.settings.graph_draft_github_token = "server-only-token"
    client.app.state.settings.graph_draft_github_max_file_bytes = 4096

    with client.app.state.db_session_factory() as session:
        repository = SQLAlchemyLabTrackerRepository(session)
        api = client.app.state.lab_tracker_api.for_request(repository)
        api.graph_drafts._configure_agentic_live_read_tools(
            draft_client,
            project_id=UUID(project_id),
            review_assignee_user_id=UUID(viewer_user_id),
        )

    assert draft_client.executor is not None
    assert draft_client.executor._github_reader is not None
    tool_names = {item["name"] for item in draft_client.executor.mcp_tool_specs()}
    assert set(GITHUB_REPOSITORY_READ_TOOL_ALLOWLIST) <= tool_names
