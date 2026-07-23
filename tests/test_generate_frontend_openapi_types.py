from __future__ import annotations

from copy import deepcopy

from scripts.generate_frontend_openapi_types import generate_declaration


def _openapi_with_response(response_schema: str) -> dict:
    return {
        "paths": {
            "/items": {
                "get": {
                    "operationId": "list_items",
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"#/components/schemas/{response_schema}"}
                                }
                            }
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "ItemEnvelope": {
                    "type": "object",
                    "properties": {"data": {"type": "string"}},
                    "required": ["data"],
                },
                "ChangedEnvelope": {
                    "type": "object",
                    "properties": {"data": {"type": "integer"}},
                    "required": ["data"],
                },
            }
        },
    }


def test_endpoint_response_model_drives_operation_and_path_declarations() -> None:
    openapi = _openapi_with_response("ItemEnvelope")
    original = generate_declaration(openapi, (("get", "/items"),))

    changed_openapi = deepcopy(openapi)
    changed_openapi["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = {"$ref": "#/components/schemas/ChangedEnvelope"}
    changed = generate_declaration(changed_openapi, (("get", "/items"),))

    assert '"/items": {' in original
    assert 'get: operations["list_items"]' in original
    assert '"application/json": components["schemas"]["ItemEnvelope"]' in original
    assert '"ChangedEnvelope"' not in original
    assert changed != original
    assert '"application/json": components["schemas"]["ChangedEnvelope"]' in changed
    assert '"ItemEnvelope"' not in changed
