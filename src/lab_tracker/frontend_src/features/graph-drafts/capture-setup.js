// Read the capture-setup tips a daily-review batch draft carries in
// context_packet.capture_setup. The server writes them after the drafter
// picks them, with its own guide copy; the packet is untyped JSON, so this
// reads it defensively and drops anything malformed instead of showing it.
// Tips are advice, not proposals: nothing here can be accepted or committed.

const MAX_TIPS = 6;
// The page links the first few captures a tip cites.
const MAX_NOTE_LINKS = 5;
const MODEL_EXPLANATION = "model";
const SERVER_EXPLANATION = "server";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SESSION_PATH_RE =
  /^\/app\/sessions\/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
// The only app pages a tip opens, with the button that opens each.
const APP_PATH_LABELS = new Map([
  ["/app", "Open Home"],
  ["/app/devices", "Open Devices"],
]);
const SESSION_PATH_LABEL = "Open the session";

function text(value) {
  return typeof value === "string" ? value : "";
}

// Each cited capture once, in the order cited. Ids are compared in lower case,
// as the server writes them, so a repeated id never gives two buttons the
// same React key.
function citedNoteIds(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  const ids = value
    .filter((noteId) => typeof noteId === "string" && UUID_RE.test(noteId))
    .map((noteId) => noteId.toLowerCase());
  return [...new Set(ids)];
}

// The button label for an app path a tip may open, or "" for any other path
// (another site, a protocol-relative URL, a page no tip names).
function appPathLabel(path) {
  if (typeof path !== "string") {
    return "";
  }
  if (APP_PATH_LABELS.has(path)) {
    return APP_PATH_LABELS.get(path);
  }
  return SESSION_PATH_RE.test(path) ? SESSION_PATH_LABEL : "";
}

function captureSetupTip(item) {
  const guide = item?.guide;
  const title = text(guide?.title);
  const steps = Array.isArray(guide?.steps)
    ? guide.steps.filter((step) => text(step).trim())
    : [];
  if (!title || steps.length === 0) {
    return null;
  }
  const noteIds = citedNoteIds(item.note_ids);
  return {
    id: text(item.recommendation_id) || title,
    title,
    sessionLabel: text(item.session_label),
    explanation: text(item.explanation),
    explanationSource:
      item.explanation_source === MODEL_EXPLANATION ? MODEL_EXPLANATION : SERVER_EXPLANATION,
    noteIds,
    steps,
    command: text(guide.command),
    appPath: appPathLabel(guide.app_path) ? guide.app_path : "",
    doc: text(guide.doc),
  };
}

// The tips to show for a change set, at most MAX_TIPS and one per id; [] when
// there are none.
function captureSetupTips(changeSet) {
  const recommendations = changeSet?.context_packet?.capture_setup?.recommendations;
  if (!Array.isArray(recommendations)) {
    return [];
  }
  const seen = new Set();
  return recommendations
    .map(captureSetupTip)
    .filter((tip) => {
      if (!tip || seen.has(tip.id)) {
        return false;
      }
      seen.add(tip.id);
      return true;
    })
    .slice(0, MAX_TIPS);
}

export { appPathLabel, captureSetupTips, MAX_NOTE_LINKS, MODEL_EXPLANATION };
