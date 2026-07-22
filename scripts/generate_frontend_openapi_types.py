"""Generate the checked frontend's TypeScript declarations from FastAPI OpenAPI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

OUTPUT_PATH = Path("src/lab_tracker/frontend_src/generated/openapi.d.ts")

# Keep this surface intentionally scoped to the gateways checked by
# tsconfig.frontend-contracts.json. Referenced component schemas are discovered
# recursively, so these declarations cannot silently drift from their enums or
# nested response models.
ROOT_SCHEMAS = (
    "AuthBootstrapStatus",
    "AuthInvitationRead",
    "AuthTokenRead",
    "AuthUserRead",
    "Dataset",
    "DeviceConsumeRead",
    "DeviceEnrollmentRead",
    "DeviceTokenRead",
    "GraphChangeOperation",
    "GraphChangeSet",
    "Note",
    "PaginationMeta",
    "PersonalAccessTokenIssuedRead",
    "PersonalAccessTokenRead",
    "Project",
    "ProjectMembership",
)


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


def generate_declaration(openapi: dict[str, Any]) -> str:
    components = openapi.get("components", {}).get("schemas", {})
    selected = _referenced_schemas(components, ROOT_SCHEMAS)
    lines = [
        "// Generated from FastAPI OpenAPI by scripts/generate_frontend_openapi_types.py.",
        "// Do not edit by hand.",
        "",
        "export interface components {",
        "  schemas: {",
    ]
    for name in sorted(selected):
        lines.append(f"    {json.dumps(name)}: {_schema_to_typescript(components[name], 4)};")
    lines.extend(["  };", "}", ""])
    return "\n".join(lines)


def _referenced_schemas(
    components: dict[str, Any], roots: tuple[str, ...]
) -> set[str]:
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
            rendered = operator.join(
                _schema_to_typescript(item, indent) for item in variants
            )
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
