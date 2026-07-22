// @ts-check

// Typed gateway for the dataset domain.
import { apiFetch, buildApiPath } from "../api.js";
import { nullish, object, optional, parseCollection, parseResource, string } from "../contract.js";

/** @typedef {import("../../generated/openapi.js").components["schemas"]["Dataset"]} Dataset */
/** @typedef {Pick<Dataset, "dataset_id"> & Partial<Pick<Dataset, "commit_hash" | "project_id" | "status">>} DatasetDto */
/** @typedef {import("../contract.js").Validator<DatasetDto>} DatasetValidator */

// A dataset as read by the dataset panel and detail card. Identity is required;
// the fields the UI branches on are type-checked when present. Nested shapes
// (question_links, commit_manifest) pass through — only their presence matters
// to the current UI, and unknown keys are preserved.
/** @type {DatasetValidator} */
const datasetShape = /** @type {DatasetValidator} */ (
  object({
    commit_hash: nullish(string),
    dataset_id: string,
    project_id: optional(string),
    status: optional(string),
  })
);

async function listDatasets(params = {}, options = {}) {
  const envelope = await apiFetch(buildApiPath("/datasets", params), options);
  return parseCollection(envelope, datasetShape);
}

/** @param {string} datasetId */
async function getDataset(datasetId, options = {}) {
  const envelope = await apiFetch(`/datasets/${datasetId}`, options);
  return parseResource(envelope, datasetShape);
}

export { datasetShape, getDataset, listDatasets };
