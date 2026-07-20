// Typed gateway for the dataset domain.
import { apiFetch, buildApiPath } from "../api.js";
import { nullish, object, optional, parseCollection, parseResource, string } from "../contract.js";

// A dataset as read by the dataset panel and detail card. Identity is required;
// the fields the UI branches on are type-checked when present. Nested shapes
// (question_links, commit_manifest) pass through — only their presence matters
// to the current UI, and unknown keys are preserved.
const datasetShape = object({
  dataset_id: string,
  project_id: optional(string),
  status: optional(string),
  commit_hash: nullish(string),
});

async function listDatasets(params = {}, options = {}) {
  const envelope = await apiFetch(buildApiPath("/datasets", params), options);
  return parseCollection(envelope, datasetShape);
}

async function getDataset(datasetId, options = {}) {
  const envelope = await apiFetch(`/datasets/${datasetId}`, options);
  return parseResource(envelope, datasetShape);
}

export { datasetShape, getDataset, listDatasets };
