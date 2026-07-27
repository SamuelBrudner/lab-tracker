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


def test_endpoint_request_model_drives_request_body_and_component_declarations() -> None:
    openapi = _openapi_with_response("ItemEnvelope")
    openapi["paths"]["/items"] = {
        "post": {
            "operationId": "create_item",
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/DataStoreCreate"}
                    }
                },
            },
            "responses": openapi["paths"]["/items"]["get"]["responses"],
        }
    }
    openapi["components"]["schemas"]["DataStoreCreate"] = {
        "type": "object",
        "properties": {
            "authority_grant_id": {
                "anyOf": [{"type": "string"}, {"type": "null"}]
            },
            "project_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "group_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "name": {"type": "string"},
            "kind": {"type": "string"},
            "root": {"type": "string"},
        },
        "required": ["name", "kind", "root"],
    }

    declaration = generate_declaration(openapi, (("post", "/items"),))

    assert "requestBody: {" in declaration
    assert '"application/json": components["schemas"]["DataStoreCreate"]' in declaration
    assert '"DataStoreCreate": {' in declaration
    assert '"authority_grant_id"?' in declaration
    assert "authority_grant_fingerprint" not in declaration

    changed_openapi = deepcopy(openapi)
    changed_openapi["components"]["schemas"]["DataStoreCreate"]["properties"][
        "authority_grant_fingerprint"
    ] = {"type": "string"}
    changed = generate_declaration(changed_openapi, (("post", "/items"),))

    assert changed != declaration
    assert '"authority_grant_fingerprint"?' in changed
