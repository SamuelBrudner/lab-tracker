// Shared vocabulary and small pure helpers for the bench capture paths: the
// kiosk scan station, NFC station tags, trusted shares, end-of-session photo
// import, voice debriefs, the hands-free shortcut, and the desktop bookmarklet.
// Every path lands a staged note through the ordinary capture routes; these
// helpers only name how it got there.

import { formatDate } from "../../shared/formatters.js";

// Metadata contract shared with the other capture packages (`capture_channel`).
const CAPTURE_CHANNEL = Object.freeze({
  BOOKMARKLET: "bookmarklet",
  DEBRIEF: "debrief",
  IMPORT: "import",
  KIOSK: "kiosk",
  NFC: "nfc",
  SHARE: "share",
  SHORTCUT: "shortcut",
});

// A capture link may only declare the channels a printed or written link can
// carry. Everything else is stamped by the page flow that makes the capture,
// never taken from a URL someone else could hand the phone.
const LINK_CAPTURE_CHANNELS = new Set([CAPTURE_CHANNEL.NFC]);

const SESSION_DEBRIEF_PURPOSE = "session_debrief";
// A scanner types a code and Enter; codes this long are not barcodes.
const BENCH_SCAN_MAX_CHARS = 256;

function readLinkCaptureChannel(search = window.location.search) {
  try {
    const value = new URLSearchParams(search || "").get("capture_channel") || "";
    return LINK_CAPTURE_CHANNELS.has(value) ? value : "";
  } catch {
    return "";
  }
}

/** Return `url` with `capture_channel` set, or `url` unchanged if it cannot be parsed. */
function withCaptureChannel(url, channel) {
  if (!url) {
    return "";
  }
  try {
    const parsed = new URL(url, window.location.origin);
    parsed.searchParams.set("capture_channel", channel);
    return parsed.toString();
  } catch {
    return url;
  }
}

function sessionTargets(sessionId) {
  return sessionId ? [{ entity_id: sessionId, entity_type: "session" }] : [];
}

function sessionLabel(session) {
  if (!session) {
    return "";
  }
  const kind = session.session_type === "scientific" ? "Scientific" : "Operational";
  return session.started_at
    ? `${kind} session started ${formatDate(session.started_at)}`
    : `${kind} session`;
}

function errorMessage(error, fallback) {
  return (error && error.message) || fallback;
}

export {
  BENCH_SCAN_MAX_CHARS,
  CAPTURE_CHANNEL,
  LINK_CAPTURE_CHANNELS,
  SESSION_DEBRIEF_PURPOSE,
  errorMessage,
  readLinkCaptureChannel,
  sessionLabel,
  sessionTargets,
  withCaptureChannel,
};
