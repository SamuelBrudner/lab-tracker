"""Generate the checked frontend's TypeScript declarations from FastAPI OpenAPI."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OUTPUT_PATH = Path("src/lab_tracker/frontend_src/generated/openapi.d.ts")

# Keep the checked surface intentionally scoped to the transport gateways. The
# declaration roots are the successful response schemas of these operations,
# rather than a hand-maintained component list. Consequently, changing an
# endpoint's response_model changes the generated operation type and trips the
# drift gate even if the old component schema still exists in OpenAPI.
SCOPED_OPERATIONS = (
    ("get", "/auth/bootstrap-status"),
    ("post", "/auth/register"),
    ("post", "/auth/login"),
    ("post", "/auth/refresh"),
    ("get", "/auth/me"),
    ("get", "/auth/setup-readiness"),
    ("get", "/auth/users"),
    ("patch", "/auth/users/{user_id}"),
    ("get", "/auth/invitations"),
    ("post", "/auth/invitations"),
    ("delete", "/auth/invitations/{invitation_id}"),
    ("get", "/auth/devices"),
    ("post", "/auth/devices/enrollment"),
    ("post", "/auth/devices/consume"),
    ("delete", "/auth/devices/{device_token_id}"),
    ("get", "/auth/tokens"),
    ("post", "/auth/tokens"),
    ("delete", "/auth/tokens/{token_id}"),
    ("get", "/projects"),
    ("get", "/projects/{project_id}/members"),
    ("get", "/projects/{project_id}/graph-draft-batch-settings"),
    ("patch", "/projects/{project_id}/graph-draft-batch-settings"),
    ("get", "/datasets"),
    ("get", "/datasets/{dataset_id}"),
    ("post", "/data-stores"),
    ("get", "/notes"),
    ("get", "/notes/{note_id}"),
    ("get", "/graph-drafts/{change_set_id}"),
    ("post", "/review-email/test"),
    ("get", "/review-email/deliveries"),
)


@dataclass(frozen=True, slots=True)
class _SelectedOperation:
    method: str
    path: str
    operation_id: str
    request_body_required: bool
    request_body: dict[str, dict[str, Any]] | None
    responses: dict[str, dict[str, dict[str, Any]]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing when the committed declaration is stale.",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)

    from lab_tracker.app import create_app

    app = create_app()
    try:
        expected = generate_declaration(app.openapi())
    finally:
        engine = getattr(app.state, "db_engine", None)
        if engine is not None:
            engine.dispose()

    current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
    if args.check:
        if current != expected:
            print(
                f"{args.output} is stale; run "
                "`uv run python scripts/generate_frontend_openapi_types.py`."
            )
            return 1
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(expected, encoding="utf-8")
    return 0


def generate_declaration(
    openapi: dict[str, Any],
    scoped_operations: tuple[tuple[str, str], ...] = SCOPED_OPERATIONS,
) -> str:
    components = openapi.get("components", {}).get("schemas", {})
    operations = _select_operations(openapi, scoped_operations)
    roots = {
        ref.rsplit("/", 1)[-1]
        for operation in operations
        for schema in _operation_schemas(operation)
        for ref in _walk_refs(schema)
    }
    selected = _referenced_schemas(components, tuple(sorted(roots)))
    lines = [
        "// Generated from FastAPI OpenAPI by scripts/generate_frontend_openapi_types.py.",
        "// Do not edit by hand.",
        "",
        "export interface paths {",
    ]
    path_operations: dict[str, list[tuple[str, str]]] = {}
    for operation in operations:
        path_operations.setdefault(operation.path, []).append(
            (operation.method, operation.operation_id)
        )
    for path, methods in path_operations.items():
        lines.append(f"  {json.dumps(path)}: {{")
        for method, operation_id in methods:
            lines.append(f"    {method}: operations[{json.dumps(operation_id)}];")
        lines.append("  };")
    lines.extend(["}", "", "export interface operations {"])
    for operation in operations:
        lines.append(f"  {json.dumps(operation.operation_id)}: {{")
        if operation.request_body is not None:
            marker = "" if operation.request_body_required else "?"
            lines.extend([f"    requestBody{marker}: {{", "      content: {"])
            for media_type, schema in operation.request_body.items():
                rendered = _schema_to_typescript(schema, 8)
                lines.append(f"        {json.dumps(media_type)}: {rendered};")
            lines.extend(["      };", "    };"])
        lines.append("    responses: {")
        for status, content in operation.responses.items():
            lines.extend([f"      {status}: {{", "        content: {"])
            for media_type, schema in content.items():
                rendered = _schema_to_typescript(schema, 10)
                lines.append(f"          {json.dumps(media_type)}: {rendered};")
            lines.extend(["        };", "      };"])
        lines.extend(["    };", "  };"])
    lines.extend(
        [
            "}",
            "",
            "export interface components {",
            "  schemas: {",
        ]
    )
    for name in sorted(selected):
        lines.append(f"    {json.dumps(name)}: {_schema_to_typescript(components[name], 4)};")
    lines.extend(["  };", "}", ""])
    return "\n".join(lines)


def _select_operations(
    openapi: dict[str, Any], scoped_operations: tuple[tuple[str, str], ...]
) -> list[_SelectedOperation]:
    paths = openapi.get("paths") or {}
    selected = []
    seen_ids: set[str] = set()
    for method, path in scoped_operations:
        path_item = paths.get(path)
        operation = path_item.get(method) if isinstance(path_item, dict) else None
        if not isinstance(operation, dict):
            raise ValueError(f"OpenAPI operation {method.upper()} {path} is missing")
        operation_id = operation.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError(f"OpenAPI operation {method.upper()} {path} has no operationId")
        if operation_id in seen_ids:
            raise ValueError(f"Duplicate OpenAPI operationId {operation_id!r}")
        seen_ids.add(operation_id)
        responses = _successful_response_schemas(operation, method=method, path=path)
        request_body_required, request_body = _request_body_schemas(
            operation,
            method=method,
            path=path,
        )
        selected.append(
            _SelectedOperation(
                method=method,
                path=path,
                operation_id=operation_id,
                request_body_required=request_body_required,
                request_body=request_body,
                responses=responses,
            )
        )
    return selected


def _request_body_schemas(
    operation: dict[str, Any],
    *,
    method: str,
    path: str,
) -> tuple[bool, dict[str, dict[str, Any]] | None]:
    request_body = operation.get("requestBody")
    if request_body is None:
        return False, None
    if not isinstance(request_body, dict):
        raise ValueError(f"OpenAPI operation {method.upper()} {path} has an invalid requestBody")
    content = request_body.get("content") or {}
    schemas = {
        media_type: media["schema"]
        for media_type, media in sorted(content.items())
        if isinstance(media, dict) and isinstance(media.get("schema"), dict)
    }
    if not schemas:
        raise ValueError(
            f"OpenAPI operation {method.upper()} {path} has no typed request body"
        )
    return request_body.get("required") is True, schemas


def _operation_schemas(operation: _SelectedOperation):
    if operation.request_body is not None:
        yield from operation.request_body.values()
    for response in operation.responses.values():
        yield from response.values()


def _successful_response_schemas(
    operation: dict[str, Any], *, method: str, path: str
) -> dict[str, dict[str, dict[str, Any]]]:
    selected: dict[str, dict[str, dict[str, Any]]] = {}
    responses = operation.get("responses") or {}
    for status, response in sorted(responses.items(), key=lambda item: item[0]):
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            continue
        if not 200 <= status_code < 300 or not isinstance(response, dict):
            continue
        content = response.get("content") or {}
        schemas = {
            media_type: media["schema"]
            for media_type, media in sorted(content.items())
            if isinstance(media, dict) and isinstance(media.get("schema"), dict)
        }
        if schemas:
            selected[str(status)] = schemas
    if not selected:
        raise ValueError(
            f"OpenAPI operation {method.upper()} {path} has no typed successful response"
        )
    return selected


def _referenced_schemas(components: dict[str, Any], roots: tuple[str, ...]) -> set[str]:
    selected: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        schema = components.get(name)
        if not isinstance(schema, dict):
            raise ValueError(f"OpenAPI schema {name!r} is missing")
        selected.add(name)
        for ref in _walk_refs(schema):
            ref_name = ref.rsplit("/", 1)[-1]
            if ref_name not in selected:
                pending.append(ref_name)
    return selected


def _walk_refs(value: Any):
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            yield ref
        for nested in value.values():
            yield from _walk_refs(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_refs(nested)


def _schema_to_typescript(schema: dict[str, Any], indent: int = 0) -> str:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        return f'components["schemas"][{json.dumps(name)}]'

    for keyword, operator in (("anyOf", " | "), ("oneOf", " | "), ("allOf", " & ")):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            rendered = operator.join(_schema_to_typescript(item, indent) for item in variants)
            return f"({rendered})"

    enum = schema.get("enum")
    if isinstance(enum, list):
        return " | ".join(json.dumps(item) for item in enum) or "never"

    schema_type = schema.get("type")
    if schema_type == "string":
        result = "string"
    elif schema_type in {"integer", "number"}:
        result = "number"
    elif schema_type == "boolean":
        result = "boolean"
    elif schema_type == "null":
        result = "null"
    elif schema_type == "array":
        item_type = _schema_to_typescript(schema.get("items") or {}, indent)
        result = f"Array<{item_type}>"
    elif schema_type == "object" or "properties" in schema or "additionalProperties" in schema:
        result = _object_to_typescript(schema, indent)
    else:
        result = "unknown"

    if schema.get("nullable") is True and result != "null":
        return f"({result} | null)"
    return result


def _object_to_typescript(schema: dict[str, Any], indent: int) -> str:
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    additional = schema.get("additionalProperties")
    if not properties:
        if isinstance(additional, dict):
            return f"Record<string, {_schema_to_typescript(additional, indent)}>"
        return "Record<string, unknown>"

    child_indent = indent + 2
    lines = ["{"]
    for name in sorted(properties):
        marker = "" if name in required else "?"
        value_type = _schema_to_typescript(properties[name], child_indent)
        lines.append(f"{' ' * child_indent}{json.dumps(name)}{marker}: {value_type};")
    lines.append(f"{' ' * indent}}}")
    result = "\n".join(lines)
    if additional is True:
        return f"({result} & Record<string, unknown>)"
    if isinstance(additional, dict):
        value_type = _schema_to_typescript(additional, indent)
        return f"({result} & Record<string, {value_type}>)"
    return result


if __name__ == "__main__":
    raise SystemExit(main())
