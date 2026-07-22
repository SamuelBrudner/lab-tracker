// Generated from FastAPI OpenAPI by scripts/generate_frontend_openapi_types.py.
// Do not edit by hand.

export interface paths {
  "/auth/bootstrap-status": {
    get: operations["auth_bootstrap_status_auth_bootstrap_status_get"];
  };
  "/auth/register": {
    post: operations["register_auth_auth_register_post"];
  };
  "/auth/login": {
    post: operations["login_auth_auth_login_post"];
  };
  "/auth/refresh": {
    post: operations["refresh_auth_auth_refresh_post"];
  };
  "/auth/me": {
    get: operations["auth_me_auth_me_get"];
  };
  "/auth/users": {
    get: operations["list_auth_users_auth_users_get"];
  };
  "/auth/users/{user_id}": {
    patch: operations["update_auth_user_auth_users__user_id__patch"];
  };
  "/auth/invitations": {
    get: operations["list_auth_invitations_auth_invitations_get"];
    post: operations["create_auth_invitation_auth_invitations_post"];
  };
  "/auth/invitations/{invitation_id}": {
    delete: operations["revoke_auth_invitation_auth_invitations__invitation_id__delete"];
  };
  "/auth/devices": {
    get: operations["list_devices_auth_devices_get"];
  };
  "/auth/devices/enrollment": {
    post: operations["create_enrollment_auth_devices_enrollment_post"];
  };
  "/auth/devices/consume": {
    post: operations["consume_enrollment_auth_devices_consume_post"];
  };
  "/auth/devices/{device_token_id}": {
    delete: operations["revoke_device_auth_devices__device_token_id__delete"];
  };
  "/auth/tokens": {
    get: operations["list_personal_access_tokens_auth_tokens_get"];
    post: operations["create_personal_access_token_auth_tokens_post"];
  };
  "/auth/tokens/{token_id}": {
    delete: operations["revoke_personal_access_token_auth_tokens__token_id__delete"];
  };
  "/projects": {
    get: operations["list_projects_projects_get"];
  };
  "/projects/{project_id}/members": {
    get: operations["list_project_members_projects__project_id__members_get"];
  };
  "/datasets": {
    get: operations["list_datasets_datasets_get"];
  };
  "/datasets/{dataset_id}": {
    get: operations["get_dataset_datasets__dataset_id__get"];
  };
  "/notes": {
    get: operations["list_notes_notes_get"];
  };
  "/notes/{note_id}": {
    get: operations["get_note_notes__note_id__get"];
  };
  "/graph-drafts/{change_set_id}": {
    get: operations["get_graph_draft_graph_drafts__change_set_id__get"];
  };
}

export interface operations {
  "auth_bootstrap_status_auth_bootstrap_status_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthBootstrapStatus_"];
        };
      };
    };
  };
  "register_auth_auth_register_post": {
    responses: {
      201: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthTokenRead_"];
        };
      };
    };
  };
  "login_auth_auth_login_post": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthTokenRead_"];
        };
      };
    };
  };
  "refresh_auth_auth_refresh_post": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthTokenRead_"];
        };
      };
    };
  };
  "auth_me_auth_me_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthUserRead_"];
        };
      };
    };
  };
  "list_auth_users_auth_users_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_AuthUserRead_"];
        };
      };
    };
  };
  "update_auth_user_auth_users__user_id__patch": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthUserRead_"];
        };
      };
    };
  };
  "list_auth_invitations_auth_invitations_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_AuthInvitationRead_"];
        };
      };
    };
  };
  "create_auth_invitation_auth_invitations_post": {
    responses: {
      201: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthInvitationRead_"];
        };
      };
    };
  };
  "revoke_auth_invitation_auth_invitations__invitation_id__delete": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_AuthInvitationRead_"];
        };
      };
    };
  };
  "list_devices_auth_devices_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_DeviceTokenRead_"];
        };
      };
    };
  };
  "create_enrollment_auth_devices_enrollment_post": {
    responses: {
      201: {
        content: {
          "application/json": components["schemas"]["Envelope_DeviceEnrollmentRead_"];
        };
      };
    };
  };
  "consume_enrollment_auth_devices_consume_post": {
    responses: {
      201: {
        content: {
          "application/json": components["schemas"]["Envelope_DeviceConsumeRead_"];
        };
      };
    };
  };
  "revoke_device_auth_devices__device_token_id__delete": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_DeviceTokenRead_"];
        };
      };
    };
  };
  "list_personal_access_tokens_auth_tokens_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_PersonalAccessTokenRead_"];
        };
      };
    };
  };
  "create_personal_access_token_auth_tokens_post": {
    responses: {
      201: {
        content: {
          "application/json": components["schemas"]["Envelope_PersonalAccessTokenIssuedRead_"];
        };
      };
    };
  };
  "revoke_personal_access_token_auth_tokens__token_id__delete": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_PersonalAccessTokenRead_"];
        };
      };
    };
  };
  "list_projects_projects_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_Project_"];
        };
      };
    };
  };
  "list_project_members_projects__project_id__members_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_ProjectMembership_"];
        };
      };
    };
  };
  "list_datasets_datasets_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_Dataset_"];
        };
      };
    };
  };
  "get_dataset_datasets__dataset_id__get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_Dataset_"];
        };
      };
    };
  };
  "list_notes_notes_get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["ListEnvelope_Note_"];
        };
      };
    };
  };
  "get_note_notes__note_id__get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_Note_"];
        };
      };
    };
  };
  "get_graph_draft_graph_drafts__change_set_id__get": {
    responses: {
      200: {
        content: {
          "application/json": components["schemas"]["Envelope_GraphChangeSet_"];
        };
      };
    };
  };
}

export interface components {
  schemas: {
    "AcceptanceMode": "human_selected" | "bulk_accepted" | "auto_accepted";
    "AuthBootstrapStatus": {
      "bootstrap_admin_configured": boolean;
      "bootstrap_token"?: (string | null);
      "bootstrap_token_warning"?: (string | null);
      "first_admin_available": boolean;
      "has_users": boolean;
    };
    "AuthInvitationRead": {
      "consumed_at"?: (string | null);
      "created_at": string;
      "email": string;
      "expires_at": string;
      "invitation_id": string;
      "invite_url"?: (string | null);
      "mailto_url"?: (string | null);
      "revoked_at"?: (string | null);
      "role": components["schemas"]["Role"];
      "status": string;
      "warning"?: (string | null);
    };
    "AuthTokenRead": {
      "access_token": string;
      "expires_at": string;
      "token_type"?: string;
      "user": components["schemas"]["AuthUserRead"];
    };
    "AuthUserRead": {
      "created_at": string;
      "role": components["schemas"]["Role"];
      "user_id": string;
      "username": string;
    };
    "Dataset": {
      "change_set_id"?: (string | null);
      "commit_hash": string;
      "commit_manifest": components["schemas"]["DatasetCommitManifest"];
      "created_at"?: string;
      "created_by"?: (string | null);
      "created_by_user_id"?: (string | null);
      "dataset_id": string;
      "origin"?: components["schemas"]["EntityOrigin"];
      "origin_model"?: (string | null);
      "origin_prompt_version"?: (string | null);
      "origin_provider"?: (string | null);
      "primary_question_id": string;
      "project_id": string;
      "question_links": Array<components["schemas"]["QuestionLink"]>;
      "status"?: components["schemas"]["DatasetStatus"];
      "terminal_reason"?: (string | null);
      "updated_at"?: string;
    };
    "DatasetCommitManifest": {
      "bids_metadata"?: Record<string, string>;
      "external_artifacts"?: Array<components["schemas"]["ExternalArtifactReference"]>;
      "files"?: Array<components["schemas"]["DatasetFile"]>;
      "metadata"?: Record<string, string>;
      "note_ids"?: Array<string>;
      "nwb_metadata"?: Record<string, string>;
      "question_links"?: Array<components["schemas"]["QuestionLink"]>;
      "source_session_id"?: (string | null);
    };
    "DatasetFile": {
      "checksum": string;
      "file_id"?: (string | null);
      "path": string;
      "size_bytes"?: (number | null);
    };
    "DatasetStatus": "staged" | "committed" | "archived";
    "DeviceConsumeRead": {
      "created_at": string;
      "device_token_id": string;
      "label": string;
      "secret": string;
    };
    "DeviceEnrollmentRead": {
      "enrollment_id": string;
      "enrollment_qr_svg": string;
      "enrollment_url": string;
      "expires_at": string;
      "offer_token": string;
    };
    "DeviceTokenRead": {
      "created_at": string;
      "device_token_id": string;
      "label": string;
      "last_used_at"?: (string | null);
      "revoked_at"?: (string | null);
    };
    "EntityOrigin": "user" | "ai_suggested" | "ai_executed" | "user_revised";
    "EntityRef": {
      "entity_id": string;
      "entity_type": components["schemas"]["EntityType"];
    };
    "EntityType": "project" | "question" | "dataset" | "note" | "session" | "analysis" | "claim" | "visualization" | "goal";
    "Envelope_AuthBootstrapStatus_": {
      "data": components["schemas"]["AuthBootstrapStatus"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_AuthInvitationRead_": {
      "data": components["schemas"]["AuthInvitationRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_AuthTokenRead_": {
      "data": components["schemas"]["AuthTokenRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_AuthUserRead_": {
      "data": components["schemas"]["AuthUserRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_Dataset_": {
      "data": components["schemas"]["Dataset"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_DeviceConsumeRead_": {
      "data": components["schemas"]["DeviceConsumeRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_DeviceEnrollmentRead_": {
      "data": components["schemas"]["DeviceEnrollmentRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_DeviceTokenRead_": {
      "data": components["schemas"]["DeviceTokenRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_GraphChangeSet_": {
      "data": components["schemas"]["GraphChangeSet"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_Note_": {
      "data": components["schemas"]["Note"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_PersonalAccessTokenIssuedRead_": {
      "data": components["schemas"]["PersonalAccessTokenIssuedRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "Envelope_PersonalAccessTokenRead_": {
      "data": components["schemas"]["PersonalAccessTokenRead"];
      "meta"?: (Record<string, unknown> | null);
    };
    "ExternalArtifactKind": "entity" | "activity";
    "ExternalArtifactReference": {
      "content_hash": string;
      "kind"?: components["schemas"]["ExternalArtifactKind"];
      "locator"?: (string | null);
      "metadata"?: Record<string, unknown>;
      "source_system": string;
      "store_name"?: (string | null);
      "uri": string;
    };
    "GraphChangeOp": "create" | "update";
    "GraphChangeOperation": {
      "acceptance_mode"?: (components["schemas"]["AcceptanceMode"] | null);
      "accepted_at"?: (string | null);
      "accepted_by"?: (string | null);
      "accepted_by_user_id"?: (string | null);
      "change_set_id": string;
      "client_ref"?: (string | null);
      "confidence"?: (number | null);
      "created_at"?: string;
      "entity_type": components["schemas"]["EntityType"];
      "error_metadata"?: Record<string, unknown>;
      "op": components["schemas"]["GraphChangeOp"];
      "operation_id": string;
      "payload"?: Record<string, unknown>;
      "rationale"?: string;
      "result_entity_id"?: (string | null);
      "review_note"?: (string | null);
      "semantic_type"?: (components["schemas"]["GraphDraftSemanticType"] | null);
      "sequence": number;
      "source_refs"?: Array<Record<string, unknown>>;
      "status"?: components["schemas"]["GraphChangeOperationStatus"];
      "target_entity_id"?: (string | null);
      "updated_at"?: string;
    };
    "GraphChangeOperationStatus": "proposed" | "accepted" | "rejected" | "applied" | "failed";
    "GraphChangeSet": {
      "batch_key"?: (string | null);
      "batch_window_end"?: (string | null);
      "batch_window_start"?: (string | null);
      "change_set_id": string;
      "clarification_requests"?: Array<string>;
      "commit_message"?: (string | null);
      "committed_at"?: (string | null);
      "committed_by"?: (string | null);
      "committed_by_username"?: (string | null);
      "context_packet"?: Record<string, unknown>;
      "created_at"?: string;
      "created_by"?: (string | null);
      "created_by_user_id"?: (string | null);
      "created_by_username"?: (string | null);
      "draft_mode"?: components["schemas"]["GraphDraftMode"];
      "error_metadata"?: Record<string, unknown>;
      "meeting_note_count": number;
      "model": string;
      "operation_count"?: number;
      "operations"?: Array<components["schemas"]["GraphChangeOperation"]>;
      "project_id": string;
      "prompt_version": string;
      "provider"?: string;
      "review_assignee"?: (string | null);
      "review_assignee_user_id"?: (string | null);
      "review_assignee_username"?: (string | null);
      "review_note"?: (string | null);
      "reviewed_at"?: (string | null);
      "reviewed_by"?: (string | null);
      "reviewed_by_username"?: (string | null);
      "source_checksum"?: (string | null);
      "source_content_type"?: (string | null);
      "source_filename"?: (string | null);
      "source_note_count": number;
      "source_note_id": string;
      "source_note_ids"?: Array<string>;
      "status"?: components["schemas"]["GraphChangeSetStatus"];
      "submitted_at"?: (string | null);
      "submitted_by"?: (string | null);
      "submitted_by_username"?: (string | null);
      "summary"?: string;
      "uncertain_fields"?: Array<string>;
      "updated_at"?: string;
    };
    "GraphChangeSetStatus": "drafting" | "ready" | "submitted" | "changes_requested" | "committing" | "rejected" | "failed" | "committed";
    "GraphDraftMode": "graph_context" | "image_only" | "graph_batch";
    "GraphDraftSemanticType": "create_entity" | "update_entity" | "create_note" | "link_note_to_question" | "link_note_to_session" | "link_note_to_dataset" | "link_note_to_analysis" | "suggest_new_question" | "suggest_new_dataset" | "suggest_new_goal" | "link_node_to_goal" | "update_goal" | "suggest_followup" | "request_clarification";
    "ListEnvelope_AuthInvitationRead_": {
      "data": Array<components["schemas"]["AuthInvitationRead"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_AuthUserRead_": {
      "data": Array<components["schemas"]["AuthUserRead"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_Dataset_": {
      "data": Array<components["schemas"]["Dataset"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_DeviceTokenRead_": {
      "data": Array<components["schemas"]["DeviceTokenRead"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_Note_": {
      "data": Array<components["schemas"]["Note"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_PersonalAccessTokenRead_": {
      "data": Array<components["schemas"]["PersonalAccessTokenRead"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_ProjectMembership_": {
      "data": Array<components["schemas"]["ProjectMembership"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "ListEnvelope_Project_": {
      "data": Array<components["schemas"]["Project"]>;
      "meta": components["schemas"]["PaginationMeta"];
    };
    "Note": {
      "archived_at"?: (string | null);
      "archived_by"?: (string | null);
      "archived_by_user_id"?: (string | null);
      "archived_reason"?: (components["schemas"]["NoteArchiveReason"] | null);
      "change_set_id"?: (string | null);
      "client_capture_id"?: (string | null);
      "created_at"?: string;
      "created_by"?: (string | null);
      "created_by_user_id"?: (string | null);
      "metadata"?: Record<string, string>;
      "note_id": string;
      "origin"?: components["schemas"]["EntityOrigin"];
      "origin_model"?: (string | null);
      "origin_prompt_version"?: (string | null);
      "origin_provider"?: (string | null);
      "project_id": string;
      "raw_asset"?: (components["schemas"]["NoteRawAsset"] | null);
      "raw_content": string;
      "status"?: components["schemas"]["NoteStatus"];
      "targets"?: Array<components["schemas"]["EntityRef"]>;
      "transcribed_text"?: (string | null);
      "updated_at"?: string;
    };
    "NoteArchiveReason": "reviewed_not_relevant" | "archived_unreviewed" | "superseded";
    "NoteRawAsset": {
      "checksum": string;
      "content_type": string;
      "filename": string;
      "size_bytes": number;
      "storage_id": string;
    };
    "NoteStatus": "staged" | "committed" | "archived";
    "OutcomeStatus": "unknown" | "supports" | "refutes" | "inconclusive";
    "PaginationMeta": {
      "limit": number;
      "offset": number;
      "total": number;
    };
    "PersonalAccessTokenIssuedRead": {
      "created_at": string;
      "expires_at": string;
      "label": string;
      "last_used_at"?: (string | null);
      "read_only": boolean;
      "revoked_at"?: (string | null);
      "role": components["schemas"]["Role"];
      "scope": string;
      "secret": string;
      "token_id": string;
    };
    "PersonalAccessTokenRead": {
      "created_at": string;
      "expires_at": string;
      "label": string;
      "last_used_at"?: (string | null);
      "read_only": boolean;
      "revoked_at"?: (string | null);
      "role": components["schemas"]["Role"];
      "scope": string;
      "token_id": string;
    };
    "Project": {
      "client_capture_id"?: (string | null);
      "created_at"?: string;
      "created_by"?: (string | null);
      "created_by_user_id"?: (string | null);
      "description"?: string;
      "group_id"?: (string | null);
      "name": string;
      "project_id": string;
      "status"?: components["schemas"]["ProjectStatus"];
      "updated_at"?: string;
    };
    "ProjectMembership": {
      "created_at"?: string;
      "created_by"?: (string | null);
      "created_by_user_id"?: (string | null);
      "membership_id": string;
      "project_id": string;
      "role": components["schemas"]["ProjectMembershipRole"];
      "updated_at"?: string;
      "user_global_role"?: (string | null);
      "user_id": string;
      "username"?: (string | null);
    };
    "ProjectMembershipRole": "viewer" | "contributor" | "owner";
    "ProjectStatus": "active" | "archived";
    "QuestionLink": {
      "outcome_status"?: components["schemas"]["OutcomeStatus"];
      "question_id": string;
      "role": components["schemas"]["QuestionLinkRole"];
    };
    "QuestionLinkRole": "primary" | "secondary";
    "Role": "admin" | "editor" | "viewer";
  };
}
