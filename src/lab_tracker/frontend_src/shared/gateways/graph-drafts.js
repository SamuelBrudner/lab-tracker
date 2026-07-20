// Typed gateway for the graph-draft (change-set) domain.
import { apiFetch } from "../api.js";
import { arrayOf, object, optional, parseResource, string, unknown } from "../contract.js";

// One proposed operation inside a change set. Identity is required; the fields
// the review UI branches on are type-checked when present, and the payload /
// source_refs bags pass through untouched.
const operationShape = object({
  operation_id: string,
  status: optional(string),
  op: optional(string),
  entity_type: optional(string),
  payload: optional(unknown),
});

// A graph-draft change set as read by the review screen. change_set_id and the
// operations array are load-bearing, so both are required; a drifted payload
// that dropped operations to a non-array now fails loudly instead of crashing
// the operations map downstream.
const changeSetShape = object({
  change_set_id: string,
  operations: arrayOf(operationShape),
  status: optional(string),
  project_id: optional(string),
});

async function getChangeSet(changeSetId, options = {}) {
  const envelope = await apiFetch(`/graph-drafts/${changeSetId}`, options);
  return parseResource(envelope, changeSetShape);
}

export { changeSetShape, getChangeSet, operationShape };
