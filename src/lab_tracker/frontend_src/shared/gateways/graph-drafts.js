// @ts-check

// Typed gateway for the graph-draft (change-set) domain.
import { apiFetch } from "../api.js";
import {
  arrayOf,
  object,
  oneOf,
  optional,
  parseResource,
  string,
  unknown,
} from "../contract.js";

/** @typedef {import("../../generated/openapi.js").operations["get_graph_draft_graph_drafts__change_set_id__get"]["responses"][200]["content"]["application/json"]["data"]} GraphChangeSet */
/** @typedef {NonNullable<GraphChangeSet["operations"]>[number]} GraphChangeOperation */
/** @typedef {Pick<GraphChangeOperation, "operation_id"> & Partial<Pick<GraphChangeOperation, "entity_type" | "op" | "status">> & {payload?: unknown}} OperationDto */
/** @typedef {Pick<GraphChangeSet, "change_set_id"> & {operations: OperationDto[]} & Partial<Pick<GraphChangeSet, "project_id" | "status">>} ChangeSetDto */
/** @typedef {import("../contract.js").Validator<OperationDto>} OperationValidator */
/** @typedef {import("../contract.js").Validator<ChangeSetDto>} ChangeSetValidator */

const entityTypeShape = /** @type {import("../contract.js").Validator<NonNullable<GraphChangeOperation["entity_type"]>>} */ (
  oneOf(
    "project",
    "question",
    "dataset",
    "note",
    "session",
    "analysis",
    "claim",
    "visualization",
    "goal"
  )
);
const operationKindShape = /** @type {import("../contract.js").Validator<NonNullable<GraphChangeOperation["op"]>>} */ (
  oneOf("create", "update")
);
const operationStatusShape = /** @type {import("../contract.js").Validator<NonNullable<GraphChangeOperation["status"]>>} */ (
  oneOf("proposed", "accepted", "rejected", "applied", "failed")
);
const changeSetStatusShape = /** @type {import("../contract.js").Validator<NonNullable<GraphChangeSet["status"]>>} */ (
  oneOf(
    "drafting",
    "ready",
    "submitted",
    "changes_requested",
    "committing",
    "rejected",
    "failed",
    "committed"
  )
);

// One proposed operation inside a change set. Identity is required; the fields
// the review UI branches on are type-checked when present, and the payload /
// source_refs bags pass through untouched.
/** @satisfies {OperationValidator} */
const operationShape = object({
  entity_type: optional(entityTypeShape),
  op: optional(operationKindShape),
  operation_id: string,
  payload: optional(unknown),
  status: optional(operationStatusShape),
});

// A graph-draft change set as read by the review screen. change_set_id and the
// operations array are load-bearing, so both are required; a drifted payload
// that dropped operations to a non-array now fails loudly instead of crashing
// the operations map downstream.
/** @satisfies {ChangeSetValidator} */
const changeSetShape = object({
  change_set_id: string,
  operations: arrayOf(operationShape),
  project_id: optional(string),
  status: optional(changeSetStatusShape),
});

/** @param {string} changeSetId */
async function getChangeSet(changeSetId, options = {}) {
  const envelope = await apiFetch(`/graph-drafts/${changeSetId}`, options);
  return parseResource(envelope, changeSetShape);
}

export { changeSetShape, getChangeSet, operationShape };
