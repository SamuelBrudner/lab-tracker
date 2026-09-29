// "Save to Lab Tracker" desktop bookmarklet: a pointer, not a copy. Clicking it
// on any page opens the capture page in a new window with the page's title,
// URL, and selected text prefilled; the person reads it and presses Save.
//
// The page details travel in the URL *fragment* (#lt-clip=...), which the
// browser never sends to a server, so the visited URL does not land in Lab
// Tracker's access logs before the person confirms. Credentials and obvious
// secret query values are stripped before anything reaches the composer.

const CLIP_FRAGMENT_KEY = "lt-clip";
const CLIP_TITLE_MAX_CHARS = 300;
const CLIP_URL_MAX_CHARS = 2000;
const CLIP_TEXT_MAX_CHARS = 2000;
// Query parameters whose values are treated as secrets and redacted. Keys are
// compared after splitting camelCase (authToken -> auth_token). Unambiguous
// words match as a suffix even with no separator (mytoken, clientsecret);
// short or common words (key, sig, code, auth, pwd) only as a whole word, so
// barcode= or monkey= keep their values.
const SECRET_KEY_SUFFIX =
  /(token|secret|password|passwd|passphrase|signature|credential|apikey|sessionid|sessid)s?$/;
const SECRET_KEY_WORD = /(^|[_.-])(key|sig|code|auth|authorization|pwd|session_id)s?$/;
const REDACTED = "REDACTED";

function normalizedQueryKey(key) {
  return String(key)
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/([A-Z]+)([A-Z][a-z])/g, "$1_$2")
    .toLowerCase();
}

function isSecretQueryKey(key) {
  const normalized = normalizedQueryKey(key);
  return SECRET_KEY_SUFFIX.test(normalized) || SECRET_KEY_WORD.test(normalized);
}

function bounded(value, max) {
  const text = String(value || "").trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/**
 * Drop userinfo from a URL and redact secret-looking query values. Returns ""
 * for anything that is not an http(s) URL.
 */
function sanitizeClipUrl(href) {
  let parsed;
  try {
    parsed = new URL(String(href || ""));
  } catch {
    return "";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return "";
  }
  parsed.username = "";
  parsed.password = "";
  for (const key of Array.from(parsed.searchParams.keys())) {
    if (isSecretQueryKey(key)) {
      parsed.searchParams.set(key, REDACTED);
    }
  }
  // Sign-in flows put tokens in the fragment (#access_token=...); keep plain
  // anchors (#methods) but drop any fragment that carries key=value pairs.
  if (parsed.hash.includes("=")) {
    parsed.hash = "";
  }
  let text = parsed.toString();
  if (text.length > CLIP_URL_MAX_CHARS) {
    // A cut URL is a broken pointer; fall back to the page itself.
    parsed.search = "";
    parsed.hash = "";
    text = parsed.toString();
  }
  return text.length > CLIP_URL_MAX_CHARS ? "" : text;
}

/** The clip a bookmarklet put in `hash`, or null. */
function readBookmarkletClip(hash = window.location.hash) {
  const raw = String(hash || "").replace(/^#/, "");
  if (!raw) {
    return null;
  }
  let params;
  try {
    params = new URLSearchParams(raw);
  } catch {
    return null;
  }
  if (params.get(CLIP_FRAGMENT_KEY) !== "1") {
    return null;
  }
  const clip = {
    text: bounded(params.get("text"), CLIP_TEXT_MAX_CHARS),
    title: bounded(params.get("title"), CLIP_TITLE_MAX_CHARS),
    url: sanitizeClipUrl(params.get("url")),
  };
  return clip.title || clip.url || clip.text ? clip : null;
}

/** Remove the clip fragment so a reload does not prefill the composer again. */
function clearBookmarkletFragment() {
  try {
    const url = new URL(window.location.href);
    if (!url.hash) {
      return;
    }
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}`);
  } catch {
    // Cosmetic; the composer text is already prefilled.
  }
}

function clipComposerText(clip) {
  const parts = [];
  if (clip.title) {
    parts.push(clip.title);
  }
  if (clip.url) {
    parts.push(clip.url);
  }
  if (clip.text) {
    parts.push(`“${clip.text}”`);
  }
  return parts.join("\n\n");
}

function clipMetadata(clip) {
  const metadata = {};
  if (clip?.title) {
    metadata.share_title = clip.title;
  }
  if (clip?.url) {
    metadata.share_url = clip.url;
  }
  return metadata;
}

/**
 * The javascript: URL for the bookmarklet. `captureUrl` is this instance's
 * absolute capture page URL. The script only reads the page title, location,
 * and selection, and opens a new window; it sends nothing anywhere itself.
 */
function bookmarkletSource(captureUrl) {
  const target = JSON.stringify(String(captureUrl));
  const script = [
    "(function(){",
    "var s=String(window.getSelection?window.getSelection():'').slice(0,",
    String(CLIP_TEXT_MAX_CHARS),
    ");",
    "var p=new URLSearchParams({'",
    CLIP_FRAGMENT_KEY,
    "':'1',title:String(document.title||'').slice(0,",
    String(CLIP_TITLE_MAX_CHARS),
    "),url:String(location.href).slice(0,",
    String(CLIP_URL_MAX_CHARS),
    "),text:s});",
    `window.open(${target}+'#'+p.toString(),'_blank','noopener,width=520,height=760');`,
    "})();",
  ].join("");
  return `javascript:${script}`;
}

export {
  CLIP_FRAGMENT_KEY,
  CLIP_TEXT_MAX_CHARS,
  bookmarkletSource,
  clearBookmarkletFragment,
  clipComposerText,
  clipMetadata,
  isSecretQueryKey,
  readBookmarkletClip,
  sanitizeClipUrl,
};
