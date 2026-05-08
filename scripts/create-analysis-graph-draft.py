"""Create a reviewable analysis graph draft through the Lab Tracker API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx


def main() -> int:
    args = _parse_args()
    base_url = _first_value(args.base_url, "LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL")
    if not base_url:
        _die("Set --base-url, LAB_TRACKER_BASE_URL, or LAB_TRACKER_MCP_BASE_URL.")
    if not args.note_id and not (args.project_id and args.evidence_file):
        _die("Provide either --note-id or both --project-id and --evidence-file.")

    timeout = httpx.Timeout(args.timeout_seconds)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        headers = _auth_headers(client, args)
        note_id = args.note_id
        if note_id is None:
            note_id = _create_evidence_note(client, args, headers=headers)
        draft = _post_envelope(
            client,
            f"/notes/{note_id}/analysis-graph-drafts",
            headers=headers,
        )

    print(json.dumps({"note_id": note_id, "change_set": draft}, indent=2, sort_keys=True))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a Lab Tracker analysis graph draft from CI evidence. The script creates "
            "or reuses a source note, then asks Lab Tracker to store an LLM-backed graph "
            "draft for human review."
        )
    )
    parser.add_argument("--base-url", help="Lab Tracker API base URL.")
    parser.add_argument("--project-id", help="Project ID for a new evidence note.")
    parser.add_argument("--note-id", help="Existing evidence note ID to draft from.")
    parser.add_argument("--evidence-file", type=Path, help="UTF-8 text or markdown evidence file.")
    parser.add_argument(
        "--note-status",
        default="committed",
        choices=["staged", "committed", "archived"],
        help="Status for the evidence note when --note-id is not supplied.",
    )
    parser.add_argument(
        "--metadata-json",
        type=Path,
        help="Optional JSON object with scalar metadata to attach to a new evidence note.",
    )
    parser.add_argument("--token", help="Bearer token. Defaults to LAB_TRACKER_TOKEN.")
    parser.add_argument("--username", help="Login username. Defaults to LAB_TRACKER_USERNAME.")
    parser.add_argument("--password", help="Login password. Defaults to LAB_TRACKER_PASSWORD.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def _first_value(value: str | None, *env_names: str) -> str | None:
    if value:
        return value
    for env_name in env_names:
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
    return None


def _auth_headers(client: httpx.Client, args: argparse.Namespace) -> dict[str, str]:
    token = _first_value(args.token, "LAB_TRACKER_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}

    username = _first_value(
        args.username,
        "LAB_TRACKER_USERNAME",
        "LAB_TRACKER_MCP_USERNAME",
    )
    password = _first_value(
        args.password,
        "LAB_TRACKER_PASSWORD",
        "LAB_TRACKER_MCP_PASSWORD",
    )
    if not username and not password:
        return {}
    if not username or not password:
        _die("Provide both username and password, or neither for auth-disabled local APIs.")

    login = _post_envelope(
        client,
        "/auth/login",
        json_payload={"username": username, "password": password},
        headers={},
    )
    access_token = login.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        _die("Lab Tracker login response did not include an access token.")
    return {"Authorization": f"Bearer {access_token}"}


def _create_evidence_note(
    client: httpx.Client,
    args: argparse.Namespace,
    *,
    headers: dict[str, str],
) -> str:
    evidence_path = Path(args.evidence_file)
    try:
        evidence_text = evidence_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _die(f"Could not read evidence file {evidence_path}: {exc}")
    if not evidence_text.strip():
        _die("Evidence file must not be empty.")

    metadata = _ci_metadata(evidence_path)
    metadata.update(_metadata_from_json(args.metadata_json))
    note = _post_envelope(
        client,
        "/notes",
        json_payload={
            "project_id": args.project_id,
            "raw_content": evidence_text,
            "metadata": metadata,
            "status": args.note_status,
        },
        headers=headers,
    )
    note_id = note.get("note_id")
    if not isinstance(note_id, str) or not note_id:
        _die("Lab Tracker note response did not include note_id.")
    return note_id


def _ci_metadata(evidence_path: Path) -> dict[str, str]:
    metadata = {
        "source": "ci-analysis-graph-draft",
        "evidence_path": str(evidence_path),
    }
    for env_name in (
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GITHUB_SHA",
        "GITHUB_WORKFLOW",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
    ):
        value = os.environ.get(env_name)
        if value:
            metadata[env_name.lower()] = value
    return metadata


def _metadata_from_json(path: Path | None) -> dict[str, str | bool | int | float]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"Could not load metadata JSON {path}: {exc}")
    if not isinstance(payload, dict):
        _die("Metadata JSON must be an object.")
    return {str(key): _metadata_scalar(value) for key, value in payload.items()}


def _metadata_scalar(value: Any) -> str | bool | int | float:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    return json.dumps(value, sort_keys=True)


def _post_envelope(
    client: httpx.Client,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = client.post(path, json=json_payload, headers=headers)
    if response.status_code >= 400:
        _die(f"POST {path} failed with HTTP {response.status_code}: {response.text}")
    try:
        payload = response.json()
    except ValueError as exc:
        _die(f"POST {path} returned non-JSON content: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        _die(f"POST {path} did not return a Lab Tracker envelope.")
    return payload["data"]


def _die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
