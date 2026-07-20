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

// A note's uploaded asset descriptor. Present only for binary notes; the UI
// reads content_type and filename.
const rawAssetShape = object({
  content_type: optional(string),
  filename: optional(string),
});

// A note as read by the capture panel and detail card. Identity is required;
// raw_asset is absent/null for text notes, and metadata is an opaque bag that
// the UI spreads rather than inspects.
const noteShape = object({
  note_id: string,
  project_id: optional(string),
  status: optional(string),
  raw_content: nullish(string),
  transcribed_text: nullish(string),
  raw_asset: nullish(rawAssetShape),
  metadata: nullish(unknown),
});

async function listNotes(params = {}, options = {}) {
  const envelope = await apiFetch(buildApiPath("/notes", params), options);
  return parseCollection(envelope, noteShape);
}

async function getNote(noteId, options = {}) {
  const envelope = await apiFetch(`/notes/${noteId}`, options);
  return parseResource(envelope, noteShape);
}

export { getNote, listNotes, noteShape, rawAssetShape };
