"""Generate the OpenAPI-derived section of the repo-owned Lab Tracker skill."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

BEGIN_MARKER = "<!-- BEGIN GENERATED API REFERENCE -->"
END_MARKER = "<!-- END GENERATED API REFERENCE -->"

REQUEST_SCHEMAS = (
    ("Projects", "ProjectCreate"),
    ("Questions", "QuestionCreate"),
    ("Notes", "NoteCreate"),
    ("Sessions", "SessionCreate"),
    ("Datasets", "DatasetCreate"),
    ("Analyses", "AnalysisCreate"),
    ("Claims", "ClaimCreate"),
    ("Goals", "GoalCreateFields"),
    ("Visualizations", "VisualizationCreate"),
    ("Graph Drafts", "GraphDraftCreateRequest"),
    ("Decision Context", "AssistantDecisionContextRequest"),
)

LIST_ENDPOINTS = (
    "/projects",
    "/questions",
    "/notes",
    "/sessions",
    "/datasets",
    "/analyses",
    "/claims",
    "/projects/{project_id}/goals",
    "/visualizations",
    "/graph-drafts",
    "/search",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skill-path",
        default="skills/lab-tracker/SKILL.md",
        help="Path to the repo-owned Lab Tracker skill.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the generated section is not current.",
    )
    args = parser.parse_args(argv)

    skill_path = Path(args.skill_path)
    generated = generate_reference(build_openapi_schema())
    current = skill_path.read_text(encoding="utf-8")
    updated = replace_generated_section(current, generated)
    if args.check:
        if updated != current:
            print(f"{skill_path} has a stale generated API reference.")
            return 1
        return 0
    skill_path.write_text(updated, encoding="utf-8")
    return 0


def build_openapi_schema() -> dict[str, Any]:
    from lab_tracker.app import create_app

    app = create_app()
    try:
        return app.openapi()
    finally:
        dispose = getattr(app.state, "db_engine", None)
        if dispose is not None:
            dispose.dispose()


def generate_reference(openapi: dict[str, Any]) -> str:
    components = openapi.get("components", {}).get("schemas", {})
    lines: list[str] = [
        BEGIN_MARKER,
        "## API Fields And Enums (Generated)",
        "",
        "Generated from the FastAPI OpenAPI schema. Do not edit this section by hand; "
        "run `python scripts/generate_lab_tracker_skill_reference.py`.",
        "",
        "List/search endpoints use `limit` between 1 and 200 and `offset` of 0 or "
        "greater unless an endpoint documents a narrower schema below.",
        "",
        "### Request Payloads",
    ]
    for label, schema_name in REQUEST_SCHEMAS:
        schema = components.get(schema_name)
        if not isinstance(schema, dict):
            continue
        required = tuple(schema.get("required") or ())
        lines.extend(["", f"#### {label}: `{schema_name}`"])
        if required:
            lines.append(f"- Required: {', '.join(f'`{field}`' for field in required)}")
        else:
            lines.append("- Required: none")
        for field_name, field_schema in sorted((schema.get("properties") or {}).items()):
            description = describe_schema(field_schema, components)
            marker = "required" if field_name in required else "optional"
            lines.append(f"- `{field_name}` ({marker}): {description}")

    lines.extend(["", "### List/Search Query Parameters"])
    paths = openapi.get("paths") or {}
    for path in LIST_ENDPOINTS:
        operation = (paths.get(path) or {}).get("get")
        if not isinstance(operation, dict):
            continue
        params = operation.get("parameters") or []
        lines.extend(["", f"#### `GET {path}`"])
        if not params:
            lines.append("- No query parameters.")
            continue
        for param in params:
            if not isinstance(param, dict):
                continue
            name = param.get("name")
            schema = param.get("schema") or {}
            required_text = "required" if param.get("required") else "optional"
            description = describe_schema(schema, components)
            if name == "limit" and "maximum" not in description:
                description += "; maximum 200 from shared route validation"
            if name == "offset" and "minimum" not in description:
                description += "; minimum 0 from shared route validation"
            lines.append(f"- `{name}` ({required_text}): {description}")

    lines.extend([END_MARKER, ""])
    return "\n".join(lines)


def replace_generated_section(skill_text: str, generated: str) -> str:
    if BEGIN_MARKER not in skill_text or END_MARKER not in skill_text:
        raise ValueError("Skill file does not contain generated API reference markers.")
    before, rest = skill_text.split(BEGIN_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    return before.rstrip() + "\n\n" + generated.rstrip() + "\n\n" + after.lstrip("\n")


def describe_schema(schema: dict[str, Any], components: dict[str, Any]) -> str:
    resolved = resolve_schema(schema, components)
    enum = resolved.get("enum")
    if isinstance(enum, list):
        return f"{schema_type_name(resolved, components)} enum: {', '.join(map(str, enum))}"
    any_of = resolved.get("anyOf") or resolved.get("oneOf")
    if isinstance(any_of, list):
        parts = [describe_schema(item, components) for item in any_of]
        return " | ".join(_dedupe(parts))
    schema_type = resolved.get("type")
    if schema_type == "array":
        item_schema = resolved.get("items") or {}
        return f"list[{describe_schema(item_schema, components)}]"
    detail = schema_type_name(resolved, components)
    constraints = schema_constraints(resolved)
    if constraints:
        return f"{detail}; {', '.join(constraints)}"
    return detail


def resolve_schema(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        resolved = components.get(name)
        if isinstance(resolved, dict):
            merged = dict(resolved)
            merged.setdefault("title", name)
            return merged
    return schema


def schema_type_name(schema: dict[str, Any], components: dict[str, Any]) -> str:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    title = schema.get("title")
    schema_type = schema.get("type")
    schema_format = schema.get("format")
    if schema_type == "string" and schema_format:
        return f"string({schema_format})"
    if isinstance(title, str) and title and title != schema_type:
        resolved = components.get(title)
        if isinstance(resolved, dict) and resolved.get("enum"):
            return title
    return str(schema_type or title or "object")


def schema_constraints(schema: dict[str, Any]) -> list[str]:
    constraints: list[str] = []
    for key, label in (
        ("minLength", "min length"),
        ("maxLength", "max length"),
        ("minimum", "minimum"),
        ("maximum", "maximum"),
        ("default", "default"),
    ):
        if key in schema:
            constraints.append(f"{label} {schema[key]}")
    return constraints


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


if __name__ == "__main__":
    raise SystemExit(main())
