"""Bounded GitHub reads for the project-scoped graph proposal agent."""

from __future__ import annotations

import base64
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from lab_tracker.graph_drafting import GraphDraftingError
from lab_tracker.models import DataStore, StoreKind

GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_MAX_FILE_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 500
GH_CLI_CREDENTIAL_REF = "gh-cli:github.com"

_COMMIT_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_REPO_PART_RE = re.compile(r"[A-Za-z0-9_.-]+\Z")
_SCP_GITHUB_REMOTE_RE = re.compile(
    r"(?:[^@/\s]+@)?github\.com:(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)\Z",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        return f"https://github.com/{self.slug}"


class GitHubRepositoryReader:
    """Read files from explicitly registered GitHub repositories.

    Repository relevance and user/project scope are enforced by the caller. This
    class accepts only GitHub remotes, requires immutable full commit hashes, and
    keeps the optional GitHub credential in the server process. The external
    harness receives only bounded MCP results.
    """

    def __init__(
        self,
        *,
        token: str = "",
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        credential_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        if max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self._token = token.strip()
        self.max_file_bytes = max_file_bytes
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._credential_resolver = credential_resolver or resolve_github_credential_ref
        self._credential_cache: dict[str, str] = {}

    def repository_for_store(self, store: DataStore) -> GitHubRepository | None:
        if store.kind is not StoreKind.GIT:
            return None
        return parse_github_repository(store.root)

    def list_files(
        self,
        store: DataStore,
        *,
        commit: str,
        path: str = "",
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> dict[str, Any]:
        repository = self._require_repository(store)
        revision = validate_full_commit(commit)
        directory = validate_repository_path(path, allow_empty=True)
        page_limit = _bounded_list_limit(limit)
        payload = self._get_contents(
            repository,
            commit=revision,
            path=directory,
            token=self._token_for_store(store),
        )
        raw_items = payload if isinstance(payload, list) else [payload]
        items: list[dict[str, Any]] = []
        for raw in raw_items[:page_limit]:
            if not isinstance(raw, dict):
                continue
            items.append(
                {
                    "path": str(raw.get("path") or ""),
                    "type": str(raw.get("type") or "unknown"),
                    "sha": str(raw.get("sha") or ""),
                    "size_bytes": _optional_nonnegative_int(raw.get("size")),
                }
            )
        return {
            "repository": repository.slug,
            "commit": revision,
            "path": directory,
            "items": items,
            "truncated": len(raw_items) > page_limit,
        }

    def read_file(
        self,
        store: DataStore,
        *,
        commit: str,
        path: str,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        repository = self._require_repository(store)
        revision = validate_full_commit(commit)
        file_path = validate_repository_path(path, allow_empty=False)
        requested_max = self.max_file_bytes if max_bytes is None else int(max_bytes)
        if requested_max < 1:
            raise GraphDraftingError("max_bytes must be positive.")
        requested_max = min(requested_max, self.max_file_bytes)
        payload = self._get_contents(
            repository,
            commit=revision,
            path=file_path,
            token=self._token_for_store(store),
        )
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GraphDraftingError("The requested GitHub path is not a file.")
        declared_size = _optional_nonnegative_int(payload.get("size"))
        if declared_size is not None and declared_size > self.max_file_bytes:
            raise GraphDraftingError(
                "The requested GitHub file exceeds the graph-draft read limit."
            )
        if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise GraphDraftingError("GitHub did not return inline file content.")
        try:
            content_bytes = base64.b64decode(
                payload["content"].replace("\n", ""), validate=True
            )
        except (ValueError, TypeError) as exc:
            raise GraphDraftingError("GitHub returned invalid file content.") from exc
        if len(content_bytes) > self.max_file_bytes:
            raise GraphDraftingError(
                "The requested GitHub file exceeds the graph-draft read limit."
            )
        try:
            content = content_bytes[:requested_max].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GraphDraftingError(
                "The requested GitHub file is not UTF-8 text."
            ) from exc
        return {
            "repository": repository.slug,
            "commit": revision,
            "path": file_path,
            "sha": str(payload.get("sha") or ""),
            "size_bytes": len(content_bytes),
            "content": content,
            "truncated": len(content_bytes) > requested_max,
        }

    def _require_repository(self, store: DataStore) -> GitHubRepository:
        repository = self.repository_for_store(store)
        if repository is None:
            raise GraphDraftingError(
                "The selected data store is not a supported GitHub repository."
            )
        return repository

    def _get_contents(
        self,
        repository: GitHubRepository,
        *,
        commit: str,
        path: str,
        token: str,
    ) -> Any:
        encoded_path = "/".join(quote(part, safe="") for part in path.split("/") if part)
        url = f"{GITHUB_API_BASE_URL}/repos/{repository.owner}/{repository.name}/contents"
        if encoded_path:
            url = f"{url}/{encoded_path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lab-tracker-graph-draft-reader",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(
                timeout=self._timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.get(url, headers=headers, params={"ref": commit})
        except httpx.HTTPError as exc:
            raise GraphDraftingError("GitHub repository read failed.") from exc
        if response.status_code == 404:
            raise GraphDraftingError(
                "The GitHub repository, commit, or path is unavailable to the reader."
            )
        if response.status_code in {401, 403}:
            raise GraphDraftingError(
                "GitHub denied the configured read-only repository credential."
            )
        if response.status_code != 200:
            raise GraphDraftingError(
                f"GitHub repository read failed with status {response.status_code}."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise GraphDraftingError("GitHub returned an invalid repository response.") from exc

    def _token_for_store(self, store: DataStore) -> str:
        if self._token:
            return self._token
        credential_ref = str(store.credential_ref or "").strip()
        if not credential_ref:
            return ""
        cached = self._credential_cache.get(credential_ref)
        if cached is not None:
            return cached
        token = str(self._credential_resolver(credential_ref) or "").strip()
        if not token:
            raise GraphDraftingError(
                "The registered GitHub credential reference is unavailable."
            )
        self._credential_cache[credential_ref] = token
        return token


def parse_github_repository(remote: str) -> GitHubRepository | None:
    """Return a sanitized owner/repo pair for a github.com git remote."""

    cleaned = str(remote or "").strip().rstrip("/")
    if not cleaned:
        return None
    scp_match = _SCP_GITHUB_REMOTE_RE.fullmatch(cleaned)
    if scp_match:
        return _repository_parts(scp_match.group("owner"), scp_match.group("repo"))
    parsed = urlsplit(cleaned)
    if (parsed.hostname or "").casefold() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return None
    return _repository_parts(parts[0], parts[1])


def resolve_github_credential_ref(credential_ref: str) -> str | None:
    """Resolve the one supported host-keyring credential reference.

    The token is captured in server memory and is never included in MCP output,
    subprocess arguments, or logs. Other reference schemes remain unresolved
    until an operator-provided secret backend is implemented.
    """

    if str(credential_ref or "").strip().casefold() != GH_CLI_CREDENTIAL_REF:
        return None
    creationflags = (
        subprocess.CREATE_NO_WINDOW
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
        else 0
    )
    try:
        completed = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    return token or None


def validate_full_commit(value: str) -> str:
    cleaned = str(value or "").strip()
    if not _COMMIT_RE.fullmatch(cleaned):
        raise GraphDraftingError(
            "Repository reads require a full 40- or 64-character commit hash."
        )
    return cleaned.lower()


def validate_repository_path(value: str, *, allow_empty: bool) -> str:
    cleaned = str(value or "").strip().strip("/")
    if not cleaned:
        if allow_empty:
            return ""
        raise GraphDraftingError("Repository file path is required.")
    if "\\" in cleaned or "\x00" in cleaned:
        raise GraphDraftingError("Repository path is invalid.")
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GraphDraftingError("Repository path must stay within the repository.")
    return "/".join(parts)


def _repository_parts(owner: str, repo: str) -> GitHubRepository | None:
    cleaned_owner = owner.strip()
    cleaned_repo = repo.strip()
    if cleaned_repo.casefold().endswith(".git"):
        cleaned_repo = cleaned_repo[:-4]
    if not cleaned_owner or not cleaned_repo:
        return None
    if not _REPO_PART_RE.fullmatch(cleaned_owner) or not _REPO_PART_RE.fullmatch(cleaned_repo):
        return None
    return GitHubRepository(owner=cleaned_owner, name=cleaned_repo)


def _bounded_list_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GraphDraftingError("limit must be an integer.") from exc
    if parsed < 1:
        raise GraphDraftingError("limit must be positive.")
    return min(parsed, MAX_LIST_LIMIT)


def _optional_nonnegative_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
