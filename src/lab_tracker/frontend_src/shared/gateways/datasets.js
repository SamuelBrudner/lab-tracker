// @ts-check

// Typed gateway for the dataset domain.
import { apiFetch, buildApiPath } from "../api.js";
import {
  nullish,
  object,
  oneOf,
  optional,
  parseCollection,
  parseResource,
  string,
} from "../contract.js";

/** @typedef {import("../../generated/openapi.js").operations["get_dataset_datasets__dataset_id__get"]["responses"][200]["content"]["application/json"]["data"]} ReadDataset */
/** @typedef {import("../../generated/openapi.js").operations["list_datasets_datasets_get"]["responses"][200]["content"]["application/json"]["data"][number]} ListedDataset */
/** @typedef {ReadDataset & ListedDataset} Dataset */
/** @typedef {Pick<Dataset, "dataset_id"> & Partial<Pick<Dataset, "project_id" | "status">> & {commit_hash?: Dataset["commit_hash"] | null}} DatasetDto */
/** @typedef {import("../contract.js").Validator<DatasetDto>} DatasetValidator */

const datasetStatusShape = /** @type {import("../contract.js").Validator<NonNullable<Dataset["status"]>>} */ (
  oneOf("staged", "committed", "archived")
);

// A dataset as read by the dataset panel and detail card. Identity is required;
// the fields the UI branches on are type-checked when present. Nested shapes
// (question_links, commit_manifest) pass through — only their presence matters
// to the current UI, and unknown keys are preserved.
/** @satisfies {DatasetValidator} */
const datasetShape = object({
  commit_hash: nullish(string),
  dataset_id: string,
  project_id: optional(string),
  status: optional(datasetStatusShape),
});

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
