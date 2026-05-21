"""Create a daily Lab Tracker graph review and optional HTML report."""

from __future__ import annotations

import argparse
from datetime import datetime
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
    args.project_id = _first_value(args.project_id, "LAB_TRACKER_PROJECT_ID")
    if not base_url:
        _die("Set --base-url, LAB_TRACKER_BASE_URL, or LAB_TRACKER_MCP_BASE_URL.")
    if not args.project_id:
        _die("Set --project-id or LAB_TRACKER_PROJECT_ID.")

    timeout = httpx.Timeout(args.timeout_seconds)
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as client:
        headers = _auth_headers(client, args)
        review = _create_daily_review(client, args, headers=headers)
        change_sets = _fetch_review_change_sets(client, review, headers=headers)
        notes, raw_assets = _fetch_source_context(client, change_sets, headers=headers)

    output_path = str(args.output) if args.output else ""
    if args.output:
        context = build_review_report_context(
            change_sets=change_sets,
            source_notes=notes,
            raw_assets=raw_assets,
            app_base_url=args.app_base_url or base_url,
        )
        model_report = _stored_review_brief(review) or _draft_model_report(args, context)
        html = render_review_report_html(context=context, model_report=model_report)
        args.output.write_text(html, encoding="utf-8")

    review_id = review.get("review_id")
    review_url = _join_url(args.app_base_url or base_url, f"/app/daily-reviews/{review_id}")
    print(
        json.dumps(
            {
                "review_id": review_id,
                "status": review.get("status"),
                "summary": review.get("summary"),
                "draft_count": len(change_sets),
                "review_url": review_url,
                "output": output_path,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create an idempotent daily graph review for a project. Lab Tracker will gather "
            "eligible notes in the review window, create missing graph drafts, and store a "
            "daily review run that links the resulting drafts for human review."
        )
    )
    parser.add_argument("--base-url", help="Lab Tracker API base URL.")
    parser.add_argument(
        "--app-base-url",
        help="Base URL used for app/report links. Defaults to the API base URL.",
    )
    parser.add_argument("--project-id", help="Project ID. Defaults to LAB_TRACKER_PROJECT_ID.")
    parser.add_argument(
        "--window-start",
        help="Inclusive ISO-8601 lower bound. Defaults to the last review window end or today.",
    )
    parser.add_argument(
        "--window-end",
        help="Inclusive ISO-8601 upper bound. Defaults to the API server's current time.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional HTML report path. When omitted, only the review run is created.",
    )
    parser.add_argument("--token", help="Bearer token. Defaults to LAB_TRACKER_TOKEN.")
    parser.add_argument("--username", help="Login username. Defaults to LAB_TRACKER_USERNAME.")
    parser.add_argument("--password", help="Login password. Defaults to LAB_TRACKER_PASSWORD.")
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--no-llm-report",
        action="store_true",
        help="Skip the LLM-written review brief when rendering --output.",
    )
    parser.add_argument(
        "--openai-api-key",
        help="OpenAI-compatible API key for the optional HTML brief.",
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


def _stored_review_brief(review: dict[str, Any]) -> dict[str, Any] | None:
    brief = review.get("review_brief")
    if isinstance(brief, dict) and brief:
        return brief
    return None


def _create_daily_review(
    client: httpx.Client,
    args: argparse.Namespace,
    *,
    headers: dict[str, str],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "project_id": args.project_id,
        "include_brief": not args.no_llm_report,
    }
    if args.window_start:
        payload["window_start"] = _iso_datetime(args.window_start, "--window-start")
    if args.window_end:
        payload["window_end"] = _iso_datetime(args.window_end, "--window-end")
    return _post_envelope(client, "/daily-graph-reviews", json_payload=payload, headers=headers)


def _fetch_review_change_sets(
    client: httpx.Client,
    review: dict[str, Any],
    *,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    change_sets = []
    for change_set_id in review.get("change_set_ids") or []:
        change_set = _get_data(client, f"/graph-drafts/{change_set_id}", headers=headers)
        if isinstance(change_set, dict):
            change_sets.append(change_set)
    return change_sets


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
    if args.no_llm_report or not args.output:
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
    return {
        "generated_at": context.get("generated_at"),
        "draft_count": context.get("draft_count"),
        "drafts": [
            {
                "change_set_id": draft.get("change_set_id"),
                "status": draft.get("status"),
                "prompt_version": draft.get("prompt_version"),
                "source_note_id": draft.get("source_note_id"),
                "source_summary": str((draft.get("source_note") or {}).get("raw_content") or "")[
                    :4000
                ],
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
            for draft in context.get("drafts") or []
        ],
    }


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


def _first_value(value: str | None, *env_names: str) -> str | None:
    if value:
        return value
    for env_name in env_names:
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
    return None


def _iso_datetime(value: str, label: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        _die(f"{label} must be an ISO-8601 datetime: {exc}")
    return value


def _join_url(base_url: str, path: str) -> str:
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _die(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
