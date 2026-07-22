// @ts-check

// Typed gateway for the note domain.
import { apiFetch, buildApiPath } from "../api.js";
import {
  nullish,
  object,
  optional,
  parseCollection,
  parseResource,
  string,
  unknown,
} from "../contract.js";

/** @typedef {import("../../generated/openapi.js").components["schemas"]["Note"]} Note */
/** @typedef {import("../../generated/openapi.js").components["schemas"]["NoteRawAsset"]} NoteRawAsset */
/** @typedef {Pick<Note, "note_id"> & Partial<Pick<Note, "metadata" | "project_id" | "raw_asset" | "raw_content" | "status" | "transcribed_text">>} NoteDto */
/** @typedef {import("../contract.js").Validator<NoteDto>} NoteValidator */
/** @typedef {import("../contract.js").Validator<Partial<NoteRawAsset>>} NoteRawAssetValidator */

// A note's uploaded asset descriptor. Present only for binary notes; the UI
// reads content_type and filename.
/** @type {NoteRawAssetValidator} */
const rawAssetShape = /** @type {NoteRawAssetValidator} */ (
  object({
    content_type: optional(string),
    filename: optional(string),
  })
);

// A note as read by the capture panel and detail card. Identity is required;
// raw_asset is absent/null for text notes, and metadata is an opaque bag that
// the UI spreads rather than inspects.
/** @type {NoteValidator} */
const noteShape = /** @type {NoteValidator} */ (
  object({
    metadata: nullish(unknown),
    note_id: string,
    project_id: optional(string),
    raw_asset: nullish(rawAssetShape),
    raw_content: nullish(string),
    status: optional(string),
    transcribed_text: nullish(string),
  })
);

async function listNotes(params = {}, options = {}) {
  const envelope = await apiFetch(buildApiPath("/notes", params), options);
  return parseCollection(envelope, noteShape);
}

/** @param {string} noteId */
async function getNote(noteId, options = {}) {
  const envelope = await apiFetch(`/notes/${noteId}`, options);
  return parseResource(envelope, noteShape);
}

export { getNote, listNotes, noteShape, rawAssetShape };
