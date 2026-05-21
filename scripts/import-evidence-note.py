"""Import a generic external evidence item into Lab Tracker as a note."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sys
from typing import Any

import httpx


def main() -> int:
    args = _parse_args()
    base_url = _first_value(args.base_url, "LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL")
    args.project_id = _first_value(args.project_id, "LAB_TRACKER_PROJECT_ID")
    if not base_url:
        _die("Set --base-url, LAB_TRACKER_BASE_URL, or LAB_TRACKER_MCP_BASE_URL.")
    if not args.project_id:
        _die("Set --project-id or LAB_TRACKER_PROJECT_ID.")
    if bool(args.text) == bool(args.file):
        _die("Provide exactly one evidence source: --text or --file.")

    timeout = httpx.Timeout(args.timeout_seconds)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        headers = _auth_headers(client, args)
        if args.file:
            note = _upload_file_note(client, args, headers=headers)
        else:
            note = _create_text_note(client, args, headers=headers)

    print(json.dumps({"note": note}, indent=2, sort_keys=True))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a Lab Tracker evidence note from a generic external inbox item. "
            "The importer only stores raw evidence and source metadata; it never creates "
            "or commits graph drafts."
        )
    )
    parser.add_argument("--base-url", help="Lab Tracker API base URL.")
    parser.add_argument("--project-id", help="Project ID. Defaults to LAB_TRACKER_PROJECT_ID.")
    parser.add_argument("--text", help="Inline UTF-8 evidence text to store as note raw_content.")
    parser.add_argument("--file", type=Path, help="Evidence file to upload as the note raw asset.")
    parser.add_argument("--content-type", help="Override uploaded file content type.")
    parser.add_argument("--transcribed-text", help="Optional transcript text for uploaded audio.")
    parser.add_argument(
        "--note-status",
        default="committed",
        choices=["staged", "committed", "archived"],
        help="Status for the created evidence note.",
    )
    parser.add_argument("--source-provider", default="generic", help="External source provider.")
    parser.add_argument("--source-uri", help="Stable URI for the external source item.")
    parser.add_argument("--source-external-id", help="Provider-specific source item ID.")
    parser.add_argument("--observed-at", help="ISO-8601 time when the adapter observed the item.")
    parser.add_argument("--capture-kind", help="Evidence capture kind, for example text or file.")
    parser.add_argument("--adapter", default="generic-importer", help="Adapter name/version.")
    parser.add_argument("--title", help="Human-readable evidence title.")
    parser.add_argument(
        "--metadata-json",
        type=Path,
        help="Optional JSON object with scalar metadata to merge into the note metadata.",
    )
    parser.add_argument("--token", help="Bearer token. Defaults to LAB_TRACKER_TOKEN.")
    parser.add_argument("--username", help="Login username. Defaults to LAB_TRACKER_USERNAME.")
    parser.add_argument("--password", help="Login password. Defaults to LAB_TRACKER_PASSWORD.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args()


def _create_text_note(
    client: httpx.Client,
    args: argparse.Namespace,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    text = str(args.text or "")
    if not text.strip():
        _die("--text must not be empty.")
    metadata = _metadata_for_args(
        args,
        content_hash=_bytes_hash(text.encode("utf-8")),
        default_source_uri="inline://text",
        default_capture_kind="text",
        default_title="Imported text evidence",
    )
    return _post_envelope(
        client,
        "/notes",
        json_payload={
            "project_id": args.project_id,
            "raw_content": text,
            "metadata": metadata,
            "status": args.note_status,
        },
        headers=headers,
    )


def _upload_file_note(
    client: httpx.Client,
    args: argparse.Namespace,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    path = Path(args.file)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        _die(f"Could not read evidence file {path}: {exc}")
    if not payload:
        _die("--file must not be empty.")
    content_type = (
        args.content_type
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    metadata = _metadata_for_args(
        args,
        content_hash=_bytes_hash(payload),
        default_source_uri=str(path.resolve()),
        default_capture_kind="file",
        default_title=path.name,
    )
    data = {
        "project_id": args.project_id,
        "metadata": json.dumps(metadata, sort_keys=True),
        "status": args.note_status,
    }
    if args.transcribed_text:
        data["transcribed_text"] = args.transcribed_text
    files = {"file": (path.name, payload, content_type)}
    return _post_envelope(client, "/notes/upload-file", data=data, files=files, headers=headers)


def _metadata_for_args(
    args: argparse.Namespace,
    *,
    content_hash: str,
    default_source_uri: str,
    default_capture_kind: str,
    default_title: str,
) -> dict[str, str]:
    source_uri = args.source_uri or default_source_uri
    metadata: dict[str, str] = {
        "evidence_source_provider": args.source_provider,
        "evidence_source_uri": source_uri,
        "evidence_source_external_id": args.source_external_id or source_uri,
        "evidence_source_observed_at": args.observed_at or _utc_iso(),
        "evidence_capture_kind": args.capture_kind or default_capture_kind,
        "evidence_content_hash": content_hash,
        "evidence_adapter": args.adapter,
        "evidence_title": args.title or default_title,
    }
    metadata.update(_metadata_from_json(args.metadata_json))
    return metadata


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


def _post_envelope(
    client: httpx.Client,
    path: str,
    *,
    json_payload: dict[str, Any] | None = None,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, bytes, str]] | None = None,
    headers: dict[str, str],
) -> dict[str, Any]:
    response = client.post(path, json=json_payload, data=data, files=files, headers=headers)
    if response.status_code >= 400:
        _die(f"POST {path} failed with HTTP {response.status_code}: {response.text}")
    try:
        payload = response.json()
    except ValueError as exc:
        _die(f"POST {path} returned non-JSON content: {exc}")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        _die(f"POST {path} did not return a Lab Tracker envelope.")
    return payload["data"]


def _metadata_from_json(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _die(f"Could not read metadata JSON {path}: {exc}")
    if not isinstance(payload, dict):
        _die("--metadata-json must contain a JSON object.")
    metadata: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            _die("--metadata-json keys must be non-empty strings.")
        if not isinstance(value, (str, int, float, bool)):
            _die("--metadata-json values must be strings, numbers, or booleans.")
        metadata[key] = str(value)
    return metadata


def _first_value(value: str | None, *env_names: str) -> str | None:
    if value:
        return value
    for env_name in env_names:
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
    return None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bytes_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
