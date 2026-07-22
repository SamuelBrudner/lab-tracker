// @ts-check

// Typed gateway for the project domain. Validators describe the representative
// shapes the frontend consumes; gateway functions fetch through the transport
// layer and validate at the network boundary so a malformed 2xx payload fails
// loudly instead of degrading to null/empty data downstream.
import { apiFetch, buildApiPath, fetchAllPages } from "../api.js";
import { arrayOf, object, oneOf, parseCollection, string } from "../contract.js";

/** @typedef {import("../../generated/openapi.js").operations["list_projects_projects_get"]["responses"][200]["content"]["application/json"]["data"][number]} Project */
/** @typedef {import("../../generated/openapi.js").operations["list_project_members_projects__project_id__members_get"]["responses"][200]["content"]["application/json"]["data"][number]} ProjectMembership */
/** @typedef {Pick<Project, "name" | "project_id">} ProjectDto */
/** @typedef {Pick<ProjectMembership, "role" | "user_id">} ProjectMembershipDto */
/** @typedef {import("../contract.js").Validator<ProjectDto>} ProjectValidator */
/** @typedef {import("../contract.js").Validator<ProjectMembershipDto>} ProjectMembershipValidator */

const membershipRoleShape = /** @type {import("../contract.js").Validator<ProjectMembership["role"]>} */ (
  oneOf("viewer", "contributor", "owner")
);

// A project as read by the workspace list (project_id) and detail cards (name).
/** @satisfies {ProjectValidator} */
const projectShape = object({
  name: string,
  project_id: string,
});

// A project membership row as read by the access boundary.
/** @satisfies {ProjectMembershipValidator} */
const memberShape = object({
  role: membershipRoleShape,
  user_id: string,
});

// List every project, validating each item. Pagination stays in the transport
// layer (fetchAllPages) whose termination logic is subtle and already tested;
// this adds fail-loud item validation on top of it.
async function listProjects(options = {}) {
  const items = await fetchAllPages("/projects", options);
  return arrayOf(projectShape)(items, "projects");
}

// List a project's members. parseCollection enforces the collection envelope,
// so a non-array `data` (contract drift) throws rather than yielding [].
/** @param {string} projectId */
async function listMembers(projectId, options = {}) {
  const envelope = await apiFetch(
    buildApiPath(`/projects/${projectId}/members`, { limit: 200 }),
    options
  );
  return parseCollection(envelope, memberShape);
}

export { listMembers, listProjects, memberShape, projectShape };
