# API fields and enums

<!-- BEGIN GENERATED API REFERENCE -->
## API Fields And Enums (Generated)

Generated from the FastAPI OpenAPI schema. Do not edit this section by hand; run `python scripts/generate_lab_tracker_skill_reference.py`.

List/search endpoints use `limit` between 1 and 200 and `offset` of 0 or greater unless an endpoint documents a narrower schema below.

### Request Payloads

#### Projects: `ProjectCreate`
- Required: `name`
- `client_capture_id` (optional): string | null
- `description` (optional): string; max length 1000 | null
- `group_id` (optional): string(uuid) | null
- `name` (required): string; min length 1, max length 255
- `status` (optional): ProjectStatus enum: active, archived | null

#### Questions: `QuestionCreate`
- Required: `project_id`, `text`, `question_type`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `client_capture_id` (optional): string | null
- `hypothesis` (optional): string | null
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `parent_question_ids` (optional): list[string(uuid)] | null
- `project_id` (required): string(uuid)
- `question_type` (required): QuestionType enum: descriptive, hypothesis_driven, method_dev, other
- `status` (optional): QuestionStatus enum: staged, active, answered, abandoned, superseded | null
- `terminal_reason` (optional): string; min length 1 | null
- `text` (required): string; min length 1

#### Notes: `NoteCreate`
- Required: `project_id`, `raw_content`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `client_capture_id` (optional): string | null
- `metadata` (optional): object | null
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `project_id` (required): string(uuid)
- `raw_content` (required): string; min length 1
- `status` (optional): NoteStatus enum: staged, committed, archived | null
- `targets` (optional): list[object] | null
- `transcribed_text` (optional): string | null

#### Sessions: `SessionCreate`
- Required: `project_id`, `session_type`
- `primary_question_id` (optional): string(uuid) | null
- `project_id` (required): string(uuid)
- `session_type` (required): SessionType enum: scientific, operational
- `started_at` (optional): string(date-time) | null

#### Datasets: `DatasetCreate`
- Required: `project_id`, `primary_question_id`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `commit_hash` (optional): string | null
- `commit_manifest` (optional): object | null
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `primary_question_id` (required): string(uuid)
- `project_id` (required): string(uuid)
- `secondary_question_ids` (optional): list[string(uuid)] | null
- `status` (optional): DatasetStatus enum: staged, committed, archived | null
- `terminal_reason` (optional): string; min length 1 | null

#### Data Stores: `DataStoreCreate`
- Required: `name`, `kind`, `root`
- Semantic requirement: provide exactly one of `project_id` or `group_id`.
- Semantic requirement: provide `authority_grant_id`; it is structurally optional only so missing and mismatched grants receive the same opaque 403.
- `object_table` and `database` appear in the shared `StoreKind` enum, but registration rejects them until their adapters and secret models exist.
- `authority_grant_id` (optional): string; min length 1, max length 128 | null
- `capabilities` (optional): list[StoreCapability enum: bytes_by_path, byte_range, list, versioned_snapshot, query] | null
- `credential_ref` (optional): string; max length 255 | null
- `endpoint` (optional): string; max length 2000 | null
- `group_id` (optional): string(uuid) | null
- `is_default` (optional): boolean; default False
- `kind` (required): StoreKind enum: local_fs, ssh, s3, gcs, azure_blob, dropbox, gdrive, box, onedrive, object_table, database, http, rclone, git
- `name` (required): string; min length 1, max length 255
- `project_id` (optional): string(uuid) | null
- `root` (required): string; min length 1, max length 2000

#### Analyses: `AnalysisCreate`
- Required: `project_id`, `dataset_ids`, `method_hash`, `code_version`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `code_version` (required): string; min length 1, max length 255
- `dataset_ids` (required): list[string(uuid)]
- `environment_hash` (optional): string; max length 255 | null
- `external_artifacts` (optional): list[object] | null
- `method_hash` (required): string; min length 1, max length 255
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `project_id` (required): string(uuid)
- `status` (optional): AnalysisStatus enum: staged, committed, archived | null
- `terminal_reason` (optional): string; min length 1 | null

#### Claims: `ClaimCreate`
- Required: `project_id`, `statement`, `confidence`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `answers_question_ids` (optional): list[string(uuid)] | null
- `confidence` (required): number; minimum 0.0, maximum 100.0
- `external_citations` (optional): list[object] | null
- `falsification_criteria` (optional): string; min length 1 | null
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `project_id` (required): string(uuid)
- `refuting_outcome` (optional): string; min length 1 | null
- `statement` (required): string; min length 1
- `status` (optional): ClaimStatus enum: proposed, testing, supported, rejected | null
- `supported_by_analysis_ids` (optional): list[string(uuid)] | null
- `supported_by_dataset_ids` (optional): list[string(uuid)] | null
- `terminal_reason` (optional): string; min length 1 | null
- `verification_plan` (optional): string; min length 1 | null

#### Goals: `GoalCreateFields`
- Required: `goal_type`, `title`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `attributes` (optional): object | null
- `external_ref` (optional): string; max length 1000 | null
- `goal_type` (required): GoalType enum: paper, grant, talk, other
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `status` (optional): GoalStatus enum: planned, in_progress, submitted, accepted, abandoned | null
- `summary` (optional): string | null
- `target_date` (optional): string(date) | null
- `title` (required): string; min length 1, max length 255

#### Visualizations: `VisualizationCreate`
- Required: `analysis_id`, `viz_type`, `file_path`
- `origin` accepts only `user` (default) or `ai_executed`; `ai_suggested` and `user_revised` are reserved for the graph-draft review path and rejected.
- `analysis_id` (required): string(uuid)
- `caption` (optional): string | null
- `file_path` (required): string; min length 1, max length 1000
- `origin` (optional): EntityOrigin enum: user, ai_suggested, ai_executed, user_revised
- `related_claim_ids` (optional): list[string(uuid)] | null
- `viz_type` (required): string; min length 1, max length 40

#### Graph Drafts: `GraphDraftCreateRequest`
- Required: none
- `external_provider_acknowledged` (optional): boolean; default False
- `mode` (optional): GraphDraftMode enum: graph_context, image_only, graph_batch
- `user_hint` (optional): string; min length 1 | null

#### Member Onboarding Checkpoint: `MemberOnboardingCheckpointRequest`
- Required: `current_output_or_decision`, `live_questions`, `strongest_recent_context`, `next_move`
- `as_of` (optional): string(date-time) | null
- `current_output_or_decision` (required): string; min length 1
- `live_questions` (required): list[string; min length 1]
- `next_move` (required): string; min length 1
- `source_text` (optional): string | null
- `strongest_recent_context` (required): string; min length 1

#### Member Onboarding Manual Alignment: `MemberOnboardingManualAlignmentRequest`
- Required: `resolutions`
- `resolutions` (required): list[object]

#### Member Onboarding AI Alignment: `MemberOnboardingAiAlignmentRequest`
- Required: `external_provider_acknowledged`
- `external_provider_acknowledged` (required): boolean

#### Decision Context: `AssistantDecisionContextRequest`
- Required: `task_kind`, `query`
- `analysis_id` (optional): string(uuid) | null
- `claim_id` (optional): string(uuid) | null
- `created_by` (optional): string(uuid) | null
- `dataset_id` (optional): string(uuid) | null
- `limit` (optional): integer; minimum 1.0, maximum 100.0, default 20
- `project_id` (optional): string(uuid) | null
- `query` (required): string; min length 1
- `question_id` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `task_kind` (required): string; min length 1
- `until` (optional): string(date-time) | null
- `visualization_id` (optional): string(uuid) | null

### List/Search Query Parameters

#### `GET /projects`
- `status` (optional): ProjectStatus enum: active, archived | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /questions`
- `project_id` (optional): string(uuid) | null
- `status` (optional): QuestionStatus enum: staged, active, answered, abandoned, superseded | null
- `question_type` (optional): QuestionType enum: descriptive, hypothesis_driven, method_dev, other | null
- `search` (optional): string | null
- `q` (optional): string | null
- `created_by` (optional): string(uuid) | null
- `parent_question_id` (optional): string(uuid) | null
- `ancestor_question_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /notes`
- `project_id` (optional): string(uuid) | null
- `status` (optional): NoteStatus enum: staged, committed, archived | null
- `created_by` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `evidence_content_hash` (optional): string | null
- `target_entity_type` (optional): EntityType enum: project, question, dataset, note, session, analysis, claim, visualization, goal, exploration_node | null
- `target_entity_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /sessions`
- `project_id` (optional): string(uuid) | null
- `status` (optional): SessionStatus enum: active, closed | null
- `session_type` (optional): SessionType enum: scientific, operational | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /datasets`
- `project_id` (optional): string(uuid) | null
- `status` (optional): DatasetStatus enum: staged, committed, archived | null
- `created_by` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /data-stores`
- `project_id` (optional): string(uuid) | null
- `group_id` (optional): string(uuid) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /analyses`
- `project_id` (optional): string(uuid) | null
- `dataset_id` (optional): string(uuid) | null
- `question_id` (optional): string(uuid) | null
- `status` (optional): AnalysisStatus enum: staged, committed, archived | null
- `created_by` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `recent_first` (optional): boolean; default False
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /claims`
- `project_id` (optional): string(uuid) | null
- `status` (optional): ClaimStatus enum: proposed, testing, supported, rejected | null
- `dataset_id` (optional): string(uuid) | null
- `analysis_id` (optional): string(uuid) | null
- `created_by` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /projects/{project_id}/goals`
- `project_id` (required): string(uuid)
- `goal_type` (optional): GoalType enum: paper, grant, talk, other | null
- `status` (optional): GoalStatus enum: planned, in_progress, submitted, accepted, abandoned | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /visualizations`
- `project_id` (optional): string(uuid) | null
- `analysis_id` (optional): string(uuid) | null
- `claim_id` (optional): string(uuid) | null
- `created_by` (optional): string(uuid) | null
- `since` (optional): string(date-time) | null
- `until` (optional): string(date-time) | null
- `recent_first` (optional): boolean; default False
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /graph-drafts`
- `project_id` (optional): string(uuid) | null
- `status` (optional): GraphChangeSetStatus enum: drafting, ready, submitted, changes_requested, committing, rejected, failed, committed | null
- `source_note_id` (optional): string(uuid) | null
- `purpose` (optional): GraphDraftPurpose enum: general, member_checkpoint_alignment | null
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /projects/{project_id}/member-onboarding/owner-queue`
- `project_id` (required): string(uuid)
- `limit` (optional): integer; default 50; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /search`
- `q` (required): string
- `project_id` (optional): string(uuid) | null
- `goal_id` (optional): string(uuid) | null
- `include` (optional): string | null
- `limit` (optional): integer; default 20; maximum 200 from shared route validation
- `offset` (optional): integer; default 0; minimum 0 from shared route validation

#### `GET /projects/{project_id}/graph/overview`
- `project_id` (required): string(uuid)

#### `GET /projects/{project_id}/graph/search`
- `project_id` (required): string(uuid)
- `q` (required): string; min length 2, max length 256
- `entity_types` (optional): list[string enum: question, session, note, dataset, analysis, claim, exploration_node, visualization, goal] | null
- `statuses` (optional): list[string] | null
- `limit` (optional): integer; minimum 1, maximum 100, default 20
- `offset` (optional): integer; minimum 0, default 0

#### `GET /projects/{project_id}/graph/neighborhood/{entity_type}/{entity_id}`
- `project_id` (required): string(uuid)
- `entity_type` (required): string enum: question, session, note, dataset, analysis, claim, exploration_node, visualization, goal
- `entity_id` (required): string(uuid)
- `direction` (optional): string enum: incoming, outgoing, both
- `relationships` (optional): list[string] | null
- `node_types` (optional): list[string enum: question, session, note, dataset, analysis, claim, exploration_node, external_artifact, visualization, goal] | null
- `depth` (optional): integer; minimum 1, maximum 2, default 1
- `max_nodes` (optional): integer; minimum 1, maximum 200, default 50
- `max_edges` (optional): integer; minimum 1, maximum 500, default 100
- `include_anchor_content` (optional): boolean; default False
<!-- END GENERATED API REFERENCE -->
