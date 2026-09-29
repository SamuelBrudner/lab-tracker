// Per-device "trusted share window": while it is open, items shared from the
// OS share sheet go straight into one session instead of waiting in the share
// inbox for review. A person opens it explicitly, for a bounded time, for one
// project and session; it lives only in this browser's localStorage.
//
// It is stored under the local-draft prefix, so signing out (which clears
// every draft) or a different person signing in on this origin (which drops
// the previous owner's drafts) removes it too. Reads also check the owner and
// the project, and the window expires on its own. Every storage access is
// guarded: a browser that refuses storage simply never trusts a share.

import { DRAFT_KEY_PREFIX } from "../../hooks/useLocalDraft.js";

const SHARE_TRUST_KEY = `${DRAFT_KEY_PREFIX}share-trust`;
const SHARE_TRUST_HOURS = Object.freeze([1, 2, 4]);
const HOUR_MS = 60 * 60 * 1000;
const MAX_TRUST_MS = Math.max(...SHARE_TRUST_HOURS) * HOUR_MS;

function storage() {
  try {
    return globalThis.localStorage || null;
  } catch {
    return null;
  }
}

function removeStoredTrust() {
  try {
    storage()?.removeItem(SHARE_TRUST_KEY);
  } catch {
    // Unavailable storage holds no trust to remove.
  }
}

function parseTrust(raw) {
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw);
    if (
      !parsed ||
      typeof parsed.ownerId !== "string" ||
      typeof parsed.projectId !== "string" ||
      typeof parsed.sessionId !== "string" ||
      !Number.isFinite(parsed.expiresAt) ||
      !Number.isFinite(parsed.grantedAt)
    ) {
      return null;
    }
    return {
      expiresAt: parsed.expiresAt,
      grantedAt: parsed.grantedAt,
      ownerId: parsed.ownerId,
      projectId: parsed.projectId,
      sessionId: parsed.sessionId,
      sessionLabel: typeof parsed.sessionLabel === "string" ? parsed.sessionLabel : "",
    };
  } catch {
    return null;
  }
}

/**
 * Open a trusted share window. Returns the stored window, or null when the
 * inputs are incomplete, the duration is not one of SHARE_TRUST_HOURS, or this
 * browser would not store it (in which case nothing is trusted).
 */
function grantShareTrust({ ownerId, projectId, sessionId, sessionLabel = "", hours, now = Date.now() }) {
  if (!ownerId || !projectId || !sessionId || !SHARE_TRUST_HOURS.includes(hours)) {
    return null;
  }
  const trust = {
    expiresAt: now + hours * HOUR_MS,
    grantedAt: now,
    ownerId,
    projectId,
    sessionId,
    sessionLabel,
  };
  try {
    const store = storage();
    if (!store) {
      return null;
    }
    store.setItem(SHARE_TRUST_KEY, JSON.stringify(trust));
    // Read back: some browsers accept a write they silently drop.
    return parseTrust(store.getItem(SHARE_TRUST_KEY)) ? trust : null;
  } catch {
    return null;
  }
}

/**
 * The open trusted share window for this person and project, or null. An
 * expired, malformed, or other person's window is removed; one for another
 * project is left alone but never applies here.
 */
function readShareTrust({ ownerId, projectId, now = Date.now() }) {
  let raw;
  try {
    raw = storage()?.getItem(SHARE_TRUST_KEY) ?? null;
  } catch {
    return null;
  }
  if (!raw) {
    return null;
  }
  const trust = parseTrust(raw);
  if (
    !trust ||
    !ownerId ||
    trust.ownerId !== ownerId ||
    now >= trust.expiresAt ||
    trust.expiresAt - trust.grantedAt > MAX_TRUST_MS ||
    trust.grantedAt > now
  ) {
    removeStoredTrust();
    return null;
  }
  if (!projectId || trust.projectId !== projectId) {
    return null;
  }
  return trust;
}

function revokeShareTrust() {
  removeStoredTrust();
}

/** "1 h 5 min", "12 min", "under a minute" */
function formatTrustRemaining(ms) {
  const minutes = Math.floor(Math.max(0, ms) / 60000);
  if (minutes < 1) {
    return "under a minute";
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) {
    return `${rest} min`;
  }
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

export {
  SHARE_TRUST_HOURS,
  SHARE_TRUST_KEY,
  formatTrustRemaining,
  grantShareTrust,
  readShareTrust,
  revokeShareTrust,
};
