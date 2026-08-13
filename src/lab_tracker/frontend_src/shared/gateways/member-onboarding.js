// @ts-check

// Typed gateway for the ongoing-project member-onboarding resource.
import { apiFetch } from "../api.js";
import {
  arrayOf,
  boolean,
  integer,
  nullish,
  object,
  oneOf,
  parseCollection,
  parseResource,
  string,
} from "../contract.js";
import { changeSetShape } from "./graph-drafts.js";
import { noteShape } from "./notes.js";

/** @typedef {import("../../generated/openapi.js").operations["get_member_onboarding_projects__project_id__member_onboarding_get"]["responses"][200]["content"]["application/json"]["data"]} MemberOnboarding */
/** @typedef {import("../../generated/openapi.js").operations["put_checkpoint_projects__project_id__member_onboarding_checkpoint_put"]["requestBody"]["content"]["application/json"]} CheckpointRequest */
/** @typedef {import("../../generated/openapi.js").operations["put_manual_alignment_projects__project_id__member_onboarding_manual_alignment_put"]["requestBody"]["content"]["application/json"]} ManualAlignmentRequest */
/** @typedef {import("../../generated/openapi.js").operations["start_ai_alignment_projects__project_id__member_onboarding_ai_alignment_post"]["requestBody"]["content"]["application/json"]} AiAlignmentRequest */
/** @typedef {import("../../generated/openapi.js").operations["owner_queue_projects__project_id__member_onboarding_owner_queue_get"]["responses"][200]["content"]["application/json"]["data"][number]} OwnerQueueItem */

const roleShape = oneOf("viewer", "contributor", "owner");
const stateShape = oneOf(
  "not_started",
  "checkpoint_ready",
  "alignment_ready",
  "awaiting_owner",
  "changes_requested",
  "rejected",
  "committed",
  "capture_pending",
  "complete"
);
const capabilitiesShape = object({
  can_align: boolean,
  can_capture: boolean,
  can_commit: boolean,
  can_create_checkpoint: boolean,
  can_read: boolean,
});
const guidedFieldsShape = object({
  current_output_or_decision: string,
  live_questions: arrayOf(string),
  next_move: string,
  source_text_present: boolean,
  strongest_recent_context: string,
});
const questionResolutionShape = object({
  action: string,
  operation_id: nullish(string),
  question_id: nullish(string),
  question_index: integer,
  status: nullish(string),
});
const alignmentShape = object({
  draft: nullish(changeSetShape),
  mode: oneOf("none", "manual", "ai"),
  question_resolutions: arrayOf(questionResolutionShape),
  resolved_at: nullish(string),
});
const mapItemShape = object({
  operation_id: nullish(string),
  question_id: nullish(string),
  question_index: integer,
  source: oneOf("shared", "pending", "personal"),
  status: string,
  text: string,
});

const memberOnboardingShape = object({
  alignment: nullish(alignmentShape),
  brief_markdown: string,
  capabilities: capabilitiesShape,
  checkpoint: nullish(noteShape),
  first_capture: nullish(noteShape),
  guided_fields: nullish(guidedFieldsShape),
  map_items: arrayOf(mapItemShape),
  member_complete: boolean,
  owner_commit_pending: boolean,
  project_id: string,
  role: roleShape,
  state: stateShape,
});

const ownerQueueItemShape = object({
  accepted_operation_count: integer,
  checkpoint: noteShape,
  draft: changeSetShape,
  member_user_id: nullish(string),
  member_username: nullish(string),
  project_id: string,
});

/** @param {string} projectId @param {{token?: string}} [options] */
async function getMemberOnboarding(projectId, options = {}) {
  const envelope = await apiFetch(`/projects/${projectId}/member-onboarding`, options);
  return parseResource(envelope, memberOnboardingShape);
}

/**
 * @param {string} projectId
 * @param {CheckpointRequest} checkpoint
 * @param {{token?: string}} [options]
 */
async function putCheckpoint(projectId, checkpoint, options = {}) {
  const envelope = await apiFetch(`/projects/${projectId}/member-onboarding/checkpoint`, {
    ...options,
    body: checkpoint,
    method: "PUT",
  });
  return parseResource(envelope, memberOnboardingShape);
}

/**
 * @param {string} projectId
 * @param {AiAlignmentRequest["external_provider_acknowledged"]} externalProviderAcknowledged
 * @param {{token?: string}} [options]
 */
async function requestAiAlignment(projectId, externalProviderAcknowledged, options = {}) {
  const envelope = await apiFetch(`/projects/${projectId}/member-onboarding/ai-alignment`, {
    ...options,
    body: { external_provider_acknowledged: externalProviderAcknowledged },
    method: "POST",
  });
  return parseResource(envelope, memberOnboardingShape);
}

/**
 * @param {string} projectId
 * @param {ManualAlignmentRequest} alignment
 * @param {{token?: string}} [options]
 */
async function putManualAlignment(projectId, alignment, options = {}) {
  const envelope = await apiFetch(
    `/projects/${projectId}/member-onboarding/manual-alignment`,
    {
      ...options,
      body: alignment,
      method: "PUT",
    }
  );
  return parseResource(envelope, memberOnboardingShape);
}

/** @param {string} projectId @param {{token?: string}} [options] */
async function listOwnerQueue(projectId, options = {}) {
  const envelope = await apiFetch(
    `/projects/${projectId}/member-onboarding/owner-queue`,
    options
  );
  return parseCollection(envelope, ownerQueueItemShape);
}

export {
  getMemberOnboarding,
  listOwnerQueue,
  memberOnboardingShape,
  ownerQueueItemShape,
  putCheckpoint,
  putManualAlignment,
  requestAiAlignment,
};
