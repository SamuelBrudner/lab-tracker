// @ts-check

// Typed gateway for the note domain.
import { apiFetch, buildApiPath } from "../api.js";
import {
  boolean,
  nullish,
  object,
  oneOf,
  optional,
  parseCollection,
  parseResource,
  string,
  unknown,
} from "../contract.js";

/** @typedef {import("../../generated/openapi.js").operations["get_note_notes__note_id__get"]["responses"][200]["content"]["application/json"]["data"]} ReadNote */
/** @typedef {import("../../generated/openapi.js").operations["list_notes_notes_get"]["responses"][200]["content"]["application/json"]["data"][number]} ListedNote */
/** @typedef {ReadNote & ListedNote} Note */
/** @typedef {NonNullable<Note["raw_asset"]>} NoteRawAsset */
/** @typedef {Pick<Note, "note_id"> & Partial<Pick<Note, "project_id" | "status" | "transcribed_text">> & {metadata?: unknown, raw_asset?: Partial<NoteRawAsset> | null, raw_content?: Note["raw_content"] | null}} NoteDto */
/** @typedef {import("../contract.js").Validator<NoteDto>} NoteValidator */
/** @typedef {import("../contract.js").Validator<Partial<NoteRawAsset>>} NoteRawAssetValidator */

const noteStatusShape = /** @type {import("../contract.js").Validator<NonNullable<Note["status"]>>} */ (
  oneOf("staged", "committed", "archived")
);

// A note's uploaded asset descriptor. Text uploads keep their content in raw
// storage too; is_text tells the UI when the bounded text projection is safe.
/** @satisfies {NoteRawAssetValidator} */
const rawAssetShape = object({
  content_type: optional(string),
  filename: optional(string),
  is_text: optional(boolean),
});

// A note as read by the capture panel and detail card. Identity is required;
// raw_asset is absent/null for text notes, and metadata is an opaque bag that
// the UI spreads rather than inspects.
/** @satisfies {NoteValidator} */
const noteShape = object({
  metadata: nullish(unknown),
  note_id: string,
  project_id: optional(string),
  raw_asset: nullish(rawAssetShape),
  raw_content: nullish(string),
  status: optional(noteStatusShape),
  transcribed_text: nullish(string),
});

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
