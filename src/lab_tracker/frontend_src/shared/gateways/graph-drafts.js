// @ts-check

// Typed gateway for the graph-draft (change-set) domain.
import { apiFetch } from "../api.js";
import { arrayOf, object, optional, parseResource, string, unknown } from "../contract.js";

/** @typedef {import("../../generated/openapi.js").components["schemas"]["GraphChangeOperation"]} GraphChangeOperation */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["GraphChangeSet"]} GraphChangeSet */
/** @typedef {Pick<GraphChangeOperation, "operation_id"> & Partial<Pick<GraphChangeOperation, "entity_type" | "op" | "payload" | "status">>} OperationDto */
/** @typedef {Pick<GraphChangeSet, "change_set_id"> & {operations: OperationDto[]} & Partial<Pick<GraphChangeSet, "project_id" | "status">>} ChangeSetDto */
/** @typedef {import("../contract.js").Validator<OperationDto>} OperationValidator */
/** @typedef {import("../contract.js").Validator<ChangeSetDto>} ChangeSetValidator */

// One proposed operation inside a change set. Identity is required; the fields
// the review UI branches on are type-checked when present, and the payload /
// source_refs bags pass through untouched.
/** @type {OperationValidator} */
const operationShape = /** @type {OperationValidator} */ (
  object({
    entity_type: optional(string),
    op: optional(string),
    operation_id: string,
    payload: optional(unknown),
    status: optional(string),
  })
);

// A graph-draft change set as read by the review screen. change_set_id and the
// operations array are load-bearing, so both are required; a drifted payload
// that dropped operations to a non-array now fails loudly instead of crashing
// the operations map downstream.
/** @type {ChangeSetValidator} */
const changeSetShape = /** @type {ChangeSetValidator} */ (
  object({
    change_set_id: string,
    operations: arrayOf(operationShape),
    project_id: optional(string),
    status: optional(string),
  })
);

/** @param {string} changeSetId */
async function getChangeSet(changeSetId, options = {}) {
  const envelope = await apiFetch(`/graph-drafts/${changeSetId}`, options);
  return parseResource(envelope, changeSetShape);
}

export { changeSetShape, getChangeSet, operationShape };
