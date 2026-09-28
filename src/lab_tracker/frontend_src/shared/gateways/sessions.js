// @ts-check

// Typed gateway for sessions and their suggestions. The suggestion read is
// computed on the server and never applied there: applying one is the person's
// click, through the ordinary session create/update routes below.
import { apiFetch } from "../api.js";
import {
  arrayOf,
  nonNegativeInteger,
  nullish,
  object,
  oneOf,
  optional,
  parseResource,
  string,
} from "../contract.js";

/** @typedef {import("../../generated/openapi.js").operations["get_session_suggestions_projects__project_id__session_suggestions_get"]["responses"][200]["content"]["application/json"]["data"]} SessionSuggestionReport */
/** @typedef {NonNullable<SessionSuggestionReport["suggestions"]>[number]} SessionSuggestion */
/** @typedef {import("../../generated/openapi.js").operations["create_session_sessions_post"]["responses"][201]["content"]["application/json"]["data"]} Session */
/** @typedef {import("../../generated/openapi.js").operations["create_session_sessions_post"]["requestBody"]["content"]["application/json"]} SessionCreateBody */
/** @typedef {import("../../generated/openapi.js").operations["update_session_sessions__session_id__patch"]["requestBody"]["content"]["application/json"]} SessionUpdateBody */
/** @typedef {Pick<SessionSuggestion, "kind" | "suggestion_id" | "title"> & Partial<Pick<SessionSuggestion, "capture_count" | "capture_note_ids" | "detail" | "end_at" | "session_id" | "start_at">>} SessionSuggestionDto */
/** @typedef {Pick<SessionSuggestionReport, "project_id" | "timezone"> & {suggestions: SessionSuggestionDto[]}} SessionSuggestionReportDto */
/** @typedef {Pick<Session, "project_id" | "session_id"> & Partial<Pick<Session, "ended_at" | "started_at" | "status">>} SessionDto */

const suggestionKindShape = /** @type {import("../contract.js").Validator<SessionSuggestion["kind"]>} */ (
  oneOf("close_quiet_session", "start_session_from_captures", "start_session_from_booking")
);
const sessionStatusShape = /** @type {import("../contract.js").Validator<NonNullable<Session["status"]>>} */ (
  oneOf("active", "closed")
);

// One suggestion as the card reads it: identity, kind, and the times and ids
// Apply sends back through the session and note routes.
/** @satisfies {import("../contract.js").Validator<SessionSuggestionDto>} */
const suggestionShape = object({
  capture_count: optional(nonNegativeInteger),
  capture_note_ids: optional(arrayOf(string)),
  detail: optional(string),
  end_at: nullish(string),
  kind: suggestionKindShape,
  session_id: nullish(string),
  start_at: nullish(string),
  suggestion_id: string,
  title: string,
});

/** @satisfies {import("../contract.js").Validator<SessionSuggestionReportDto>} */
const suggestionReportShape = object({
  project_id: string,
  suggestions: arrayOf(suggestionShape),
  timezone: string,
});

/** @satisfies {import("../contract.js").Validator<SessionDto>} */
const sessionShape = object({
  ended_at: nullish(string),
  project_id: string,
  session_id: string,
  started_at: optional(string),
  status: optional(sessionStatusShape),
});

/** @param {string} projectId */
async function listSessionSuggestions(projectId, options = {}) {
  const envelope = await apiFetch(
    `/projects/${encodeURIComponent(projectId)}/session-suggestions`,
    options
  );
  return parseResource(envelope, suggestionReportShape);
}

/** @param {SessionCreateBody} body */
async function createSession(body, options = {}) {
  const envelope = await apiFetch("/sessions", { ...options, method: "POST", body });
  return parseResource(envelope, sessionShape);
}

/** @param {string} sessionId @param {SessionUpdateBody} body */
async function updateSession(sessionId, body, options = {}) {
  const envelope = await apiFetch(`/sessions/${encodeURIComponent(sessionId)}`, {
    ...options,
    method: "PATCH",
    body,
  });
  return parseResource(envelope, sessionShape);
}

export {
  createSession,
  listSessionSuggestions,
  sessionShape,
  suggestionReportShape,
  suggestionShape,
  updateSession,
};
