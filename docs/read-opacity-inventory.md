# Read Opacity Inventory

This inventory freezes the retained targeted-read surfaces where revealing
whether a record exists would disclose project or group membership. For each
variant, an existing inaccessible target and a missing target return the same
canonical `404`; authorized callers retain the documented media type and
payload contract. Authentication failures remain `401`; service-token
capability failures and the explicit permission failures of mutations are
`403` ([status-code contract](internal-boundaries.md#authentication-versus-authorization-status-codes)).

## Count and scope

| Slice | Behavioral variants | Distinct OpenAPI operations | Scope |
| --- | ---: | ---: | --- |
| Core | 19 | 19 | Projects 8; questions 6; notes 2; sessions 3 |
| Evidence and artifacts | 18 | 16 | Fifteen GET variants plus dataset, analysis, and claim modes of the artifact resolver |
| Workflow and registry | 4 | 4 | Graph-draft detail, batch-detail alias, data-store detail, and data-store health |
| Acquisition | 10 | 10 | Experiments 3; session and dataset experiment links 2; collections and snapshots 5 |
| **Total** | **51** | **49** | **19 + 18 + 4 + 10 behavioral variants** |

The behavioral count is larger than the OpenAPI-operation count because the
three entity modes all exercise the single semantic-read operation
`POST /external-artifacts/resolve`. They are separate authorization cases but
one OpenAPI path-and-method operation.

The 15 evidence GET variants comprise datasets 4, analyses 2, claims 5,
visualizations 2, exploration nodes 1, and provenance links 1. Resolver modes
add dataset, analysis, and claim. The eight core project variants are project
detail, publication readiness, draft quality, and the graph JSON, overview,
search, neighborhood, and Mermaid reads. The 19 core, four workflow/registry,
and ten acquisition variants are enumerated directly by their behavioral
suites, and
[`tests/read_opacity_inventory.py`](../tests/read_opacity_inventory.py) is the
canonical list.

## Representations and exclusions

JSON-LD provenance and ARA routes that have their own paths are already counted
above. Content-negotiated `application/ld+json` representations of dataset,
analysis, and claim detail are supplemental checks of the same three OpenAPI
operations, so they do not increase either total. Binary downloads likewise
remain the representation of their counted operation.

Lists are excluded because they filter to accessible scopes rather than return
an opaque target miss. Mutations are excluded because they preserve explicit
permission failures. Direct group detail is the adjacent group-scoped boundary
completed by `lab-tracker-n5kp.33`; missing-project graph-draft batch settings
remain outside this inventory under open `lab-tracker-n5kp.32`.

## Executable contracts

- [Core read opacity](../tests/test_core_read_opacity.py)
- [Evidence and artifact read opacity](../tests/test_evidence_artifact_read_opacity.py)
- [Workflow and registry read opacity](../tests/test_workflow_registry_read_opacity.py)
- [Acquisition read opacity](../tests/test_acquisition_read_opacity.py)
- [Internal boundary ordering](internal-boundaries.md#opaque-targeted-read-ordering)
- [Resolver-specific authorization order](external-artifact-resolution-design.md#read-surface-integration)
