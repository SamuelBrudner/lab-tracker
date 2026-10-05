# MCP tool inventory

<!-- BEGIN GENERATED MCP TOOL LIST -->
Use these tools when available. This list is generated from `lab_tracker.mcp_tools.READ_TOOLS` and `WRITE_TOOLS`; do not edit it by hand.

Read tools:
- `lab_tracker_health`: Check Lab Tracker API health; fail softly if the service is unavailable.
- `lab_tracker_readiness`: Check Lab Tracker database and storage readiness.
- `lab_tracker_describe_schema`: Describe fields/enums before create_* calls; use after context lookup.
- `lab_tracker_list_projects`: List visible projects when scoping a follow-up Lab Tracker read.
- `lab_tracker_list_questions`: List/search questions when inspecting known project/question scope.
- `lab_tracker_list_question_refactors`: List refactor history where a question is the source or replacement.
- `lab_tracker_list_notes`: List notes for known scope or by exact evidence_content_hash; use decision context first.
- `lab_tracker_search`: Search questions and notes when the project or anchor IDs are not known.
- `lab_tracker_graph_overview`: Orient within one project using bounded counts and entry-point summaries.
- `lab_tracker_search_graph`: Search all retained graph record types inside one authorized project.
- `lab_tracker_get_graph_neighborhood`: Traverse a deterministic, bounded typed neighborhood around one graph node.
- `lab_tracker_list_sessions`: List acquisition/experiment sessions for a known project scope.
- `lab_tracker_list_datasets`: List datasets; create-order is dataset -> analysis -> claim -> visualization.
- `lab_tracker_list_analyses`: List analyses; use after datasets and before claims/visualizations.
- `lab_tracker_list_claims`: List claims for known evidence; claims come after datasets and analyses.
- `lab_tracker_list_claim_edges`: List typed outgoing logic edges for a claim.
- `lab_tracker_list_visualizations`: List visualizations after resolving related analyses or claims.
- `lab_tracker_list_goals`: List goals/outputs when deciding what research objective to advance.
- `lab_tracker_get_goal`: Get one goal with node links before advancing or updating it.
- `lab_tracker_publication_readiness`: Check structural publication readiness for one project (seal_level ara_l1/blocked).
- `lab_tracker_draft_quality`: Report how AI draft proposals fared in human review for one project.
- `lab_tracker_list_node_goals`: List goals linked to one project graph node.
- `lab_tracker_get_dataset_provenance`: Get dataset provenance JSON-LD before reusing evidence.
- `lab_tracker_get_analysis_provenance`: Get analysis provenance JSON-LD before reusing derived evidence.
- `lab_tracker_get_claim_provenance`: Get claim-centric provenance JSON-LD with analysis/dataset/question ancestry.
- `lab_tracker_resolve_artifact`: Resolve a registered store pointer; direct locators remain metadata.
- `lab_tracker_export_goal_artifact`: Compile a goal into an Ara artifact; pass layer logic/src/trace/evidence for one layer.
- `lab_tracker_export_question_subtree`: Compile a question subtree into layered Ara JSON-LD.
- `lab_tracker_get_decision_context`: CALL THIS FIRST before research-facing decisions.
- `lab_tracker_next_questions`: Rank open active/staged questions on planned/in-progress goals.
- `lab_tracker_list_my_drafts`: List Daily Review drafts assigned to the token's user (the personal queue).
- `lab_tracker_get_graph_draft`: Read one graph draft and its proposed operations before deciding on it.

Write tools:
- `lab_tracker_create_project`: Create a project only when the user explicitly asks for a new scope.
- `lab_tracker_create_question`: Create a question after project/goal scope is known.
- `lab_tracker_refactor_question`: Supersede a question with a replacement and optional child/note moves.
- `lab_tracker_create_note`: Create a text note when the user asks to record source context.
- `lab_tracker_create_dataset`: Create a dataset before analyses, claims, and visualizations.
- `lab_tracker_create_analysis`: Create an analysis after datasets and before claims or figures.
- `lab_tracker_create_claim`: Create a claim after linking supporting datasets or analyses.
- `lab_tracker_create_claim_edge`: Create a typed claim-to-claim logic edge such as refutes or extends.
- `lab_tracker_create_visualization`: Register a visualization after its analysis and related claims exist.
- `lab_tracker_create_goal`: Create a goal/output before linking questions, datasets, or claims.
- `lab_tracker_update_goal`: Update a Lab Tracker goal/output.
- `lab_tracker_link_node_to_goal`: Tag an existing graph node in relation to a goal/output.
- `lab_tracker_upload_visualization_file`: Upload a local file into managed storage for a visualization node.
- `lab_tracker_request_graph_draft`: Ask the server-side model to propose graph changes from a staged note.
- `lab_tracker_run_graph_draft_batch`: Draft the project's staged notes now, as the daily review would.
- `lab_tracker_accept_graph_draft_operations`: Accept a draft's proposals under the project owner's delegated-curation grant.
- `lab_tracker_commit_graph_draft`: Commit a draft's accepted proposals under the delegated-curation grant.
- `lab_tracker_record_evidence_bundle`: Preview or atomically record an evidence bundle; defaults to dry-run.
<!-- END GENERATED MCP TOOL LIST -->
