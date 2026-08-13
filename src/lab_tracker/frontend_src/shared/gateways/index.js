// Domain gateways: the validated network boundary for each API domain. Import a
// namespace (`import { projects } from "../shared/gateways/index.js"`) and call
// e.g. `projects.listMembers(id, { token })`, or import a shape validator for
// use with useApiResource's `validate` option.
import * as auth from "./auth.js";
import * as datasets from "./datasets.js";
import * as graphDrafts from "./graph-drafts.js";
import * as memberOnboarding from "./member-onboarding.js";
import * as notes from "./notes.js";
import * as projects from "./projects.js";

export { auth, datasets, graphDrafts, memberOnboarding, notes, projects };
