"""Create an HTML review report for accumulated Lab Tracker graph drafts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx

from lab_tracker.graph_draft_reports import (
    build_review_report_context,
    render_review_report_html,
)
from lab_tracker.graph_drafting import GraphDraftingError, OpenAIGraphDraftClient


def main() -> int:
    args = _parse_args()
    base_url = _first_value(args.base_url, "LAB_TRACKER_BASE_URL", "LAB_TRACKER_MCP_BASE_URL")
    if not base_url:
        _die("Set --base-url, LAB_TRACKER_BASE_URL, or LAB_TRACKER_MCP_BASE_URL.")

    timeout = httpx.Timeout(args.timeout_seconds)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        headers = _auth_headers(client, args)
        change_sets = _fetch_graph_drafts(client, args, headers=headers)
        notes, raw_assets = _fetch_source_context(client, change_sets, headers=headers)

    context = build_review_report_context(
        change_sets=change_sets,
        source_notes=notes,
        raw_assets=raw_assets,
        app_base_url=args.app_base_url or base_url,
    )
    model_report = _draft_model_report(args, context)
    html = render_review_report_html(context=context, model_report=model_report)
    args.output.write_text(html, encoding="utf-8")
    print(json.dumps({"draft_count": len(change_sets), "output": str(args.output)}, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an end-of-day HTML review report for ready Lab Tracker graph drafts. "
            "The report includes source evidence, proposed operations, model-written "
            "review guidance when configured, and links back to Lab Tracker review pages."
        )
    )
    parser.add_argument("--base-url", help="Lab Tracker API base URL.")
    parser.add_argument(
        "--app-base-url",
        help="Base URL used for report links. Defaults to the API base URL.",
    )
    parser.add_argument("--project-id", help="Limit report to one project ID.")
    parser.add_argument("--status", default="ready", help="Graph draft status to report.")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("graph-draft-review.html"))
    parser.add_argument("--token", help="Bearer token. Defaults to LAB_TRACKER_TOKEN.")
    parser.add_argument("--username", help="Login username. Defaults to LAB_TRACKER_USERNAME.")
    parser.add_argument("--password", help="Login password. Defaults to LAB_TRACKER_PASSWORD.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument(
        "--no-llm-report",
        action="store_true",
        help="Skip the LLM-written review brief and render the evidence directly.",
    )
    parser.add_argument(
        "--openai-api-key",
        help="OpenAI-compatible API key. Defaults to LAB_TRACKER_OPENAI_API_KEY.",
    )
    parser.add_argument(
        "--openai-model",
        help="OpenAI-compatible model. Defaults to LAB_TRACKER_OPENAI_MODEL or gpt-5.4-mini.",
    )
    parser.add_argument(
        "--openai-base-url",
        default=os.environ.get("LAB_TRACKER_OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
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


def _fetch_graph_drafts(
    client: httpx.Client,
    args: argparse.Namespace,
    *,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    params = {"status": args.status, "limit": args.limit, "offset": 0}
    if args.project_id:
        params["project_id"] = args.project_id
    payload = _get_envelope(client, "/graph-drafts", params=params, headers=headers)
    data = payload.get("data")
    if not isinstance(data, list):
        _die("Lab Tracker graph draft list response did not include a data list.")
    return [item for item in data if isinstance(item, dict)]


def _fetch_source_context(
    client: httpx.Client,
    change_sets: list[dict[str, Any]],
    *,
    headers: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    notes: dict[str, dict[str, Any]] = {}
    raw_assets: dict[str, dict[str, Any]] = {}
    for change_set in change_sets:
        note_id = str(change_set.get("source_note_id") or "")
        if not note_id or note_id in notes:
            continue
        note = _get_data(client, f"/notes/{note_id}", headers=headers)
        if isinstance(note, dict):
            notes[note_id] = note
        raw_asset = note.get("raw_asset") if isinstance(note, dict) else None
        if raw_asset:
            try:
                raw = _get_data(
                    client,
                    f"/notes/{note_id}/raw",
                    headers={**headers, "Accept": "application/json"},
                )
            except SystemExit:
                raw = {}
            if isinstance(raw, dict):
                raw_assets[note_id] = raw
    return notes, raw_assets


def _draft_model_report(args: argparse.Namespace, context: dict[str, Any]) -> dict[str, Any] | None:
    if args.no_llm_report:
        return None
    api_key = _first_value(args.openai_api_key, "LAB_TRACKER_OPENAI_API_KEY")
    if not api_key:
        print(
            "LAB_TRACKER_OPENAI_API_KEY is not set; rendering report without LLM brief.",
            file=sys.stderr,
        )
        return None
    client = OpenAIGraphDraftClient(
        api_key=api_key,
        model=args.openai_model or os.environ.get("LAB_TRACKER_OPENAI_MODEL", "gpt-5.4-mini"),
        base_url=args.openai_base_url,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        return client.draft_review_report(review_context=_compact_report_context(context))
    except GraphDraftingError as exc:
        print(f"Model review brief failed: {exc}; rendering without it.", file=sys.stderr)
        return None
    finally:
        client.close()


def _compact_report_context(context: dict[str, Any]) -> dict[str, Any]:
    drafts = []
    for draft in context.get("drafts") or []:
        source_note = draft.get("source_note") or {}
        raw_content = str(source_note.get("raw_content") or "")
        raw_asset = draft.get("raw_asset") or {}
        drafts.append(
            {
                "change_set_id": draft.get("change_set_id"),
                "status": draft.get("status"),
                "prompt_version": draft.get("prompt_version"),
                "source_note_id": draft.get("source_note_id"),
                "source_summary": raw_content[:4000],
                "raw_asset": {
                    "filename": raw_asset.get("filename"),
                    "content_type": raw_asset.get("content_type"),
                    "checksum": raw_asset.get("checksum"),
                },
                "operations": [
                    {
                        "op": operation.get("op"),
                        "entity_type": operation.get("entity_type"),
                        "status": operation.get("status"),
                        "rationale": operation.get("rationale"),
                        "confidence": operation.get("confidence"),
                        "payload": operation.get("payload"),
                        "source_refs": operation.get("source_refs"),
                    }
                    for operation in draft.get("operations") or []
                ],
            }
        )
    return {
        "generated_at": context.get("generated_at"),
        "draft_count": context.get("draft_count"),
        "drafts": drafts,
    }


def _get_data(
    client: httpx.Client,
    path: str,
    *,
    headers: dict[str, str],
) -> Any:
    payload = _get_envelope(client, path, headers=headers)
    return payload.get("data")


def _get_envelope(
    client: httpx.Client,
    path: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.get(path, headers=headers, params=params)
    if response.status_code >= 400:
        _die(f"GET {path} failed with HTTP {response.status_code}: {response.text}")
    try:
        payload = response.json()
    except ValueError as exc:
        _die(f"GET {path} returned non-JSON content: {exc}")
    if not isinstance(payload, dict):
        _die(f"GET {path} did not return a JSON object.")
    return payload


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
