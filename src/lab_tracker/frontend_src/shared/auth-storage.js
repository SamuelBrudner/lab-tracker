// Resilient auth-token storage adapter.
//
// Browser localStorage can throw on WRITE and REMOVE (quota exceeded, Safari
// private mode, SecurityError under some embedding/policy setups), not just on
// read. Persisting an auth token after a successful login must never escape into
// the app's top-level error boundary and sign the user out of a session that is
// otherwise valid in memory.
//
// This adapter wraps a backing store (localStorage by default) with symmetric
// guarded get/set/remove and a per-key in-memory shadow. The shadow is retained
// even after a successful backing write: if a later read starts throwing, the
// tab still has the last value it wrote. Failed writes become authoritative
// overrides so a stale backing value cannot replace a newer live token, and a
// tombstone prevents a failed logout removal from resurrecting the old token.
import {
  TOKEN_EXPIRES_AT_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from "./constants.js";

const DEFAULT_BACKING = Symbol("default-auth-backing");
const TOMBSTONE = Symbol("auth-storage-tombstone");

/**
 * @typedef {{
 *   getItem: (key: string) => string | null | undefined,
 *   setItem: (key: string, value: string) => unknown,
 *   removeItem: (key: string) => unknown,
 * }} StorageLike
 */

/** @param {StorageLike | null | typeof DEFAULT_BACKING} [backing] */
function createAuthStorage(backing = DEFAULT_BACKING) {
  /** @type {Map<string, string | typeof TOMBSTONE>} */
  const shadow = new Map();
  /** @type {Set<string>} */
  const authoritative = new Set();
  let degraded = false;
  /** @type {StorageLike | null} */
  let resolvedBacking = null;

  // Merely reading globalThis.localStorage can throw a SecurityError in locked
  // down browser contexts, so default-backing discovery belongs inside the
  // same failure boundary as the storage methods themselves.
  if (backing === DEFAULT_BACKING) {
    try {
      resolvedBacking = globalThis.localStorage ?? null;
    } catch {
      resolvedBacking = null;
      degraded = true;
    }
  } else {
    resolvedBacking = backing;
  }
  if (!resolvedBacking) {
    degraded = true;
  }

  /** @param {string} key */
  function shadowValue(key) {
    if (!shadow.has(key) || shadow.get(key) === TOMBSTONE) {
      return "";
    }
    const value = shadow.get(key);
    return typeof value === "string" ? value : "";
  }

  return {
    /** @param {string} key */
    getItem(key) {
      // A failed write/remove means the backing store is known to be stale for
      // this key. Do not let a later successful get resurrect that stale value.
      if (authoritative.has(key)) {
        return shadowValue(key);
      }
      if (resolvedBacking) {
        try {
          const value = resolvedBacking.getItem(key);
          shadow.set(key, value === null || value === undefined ? TOMBSTONE : value);
          return value === null || value === undefined ? "" : value;
        } catch {
          degraded = true;
          // Fall back to the last observed/written value below.
        }
      }
      return shadowValue(key);
    },

    /** @param {string} key @param {unknown} value */
    setItem(key, value) {
      const normalized = String(value);
      shadow.set(key, normalized);
      if (resolvedBacking) {
        try {
          resolvedBacking.setItem(key, normalized);
          authoritative.delete(key);
          return true;
        } catch {
          degraded = true;
        }
      }
      authoritative.add(key);
      degraded = true;
      return false;
    },

    /** @param {string} key */
    removeItem(key) {
      shadow.set(key, TOMBSTONE);
      authoritative.add(key);
      if (resolvedBacking) {
        try {
          resolvedBacking.removeItem(key);
          authoritative.delete(key);
          return true;
        } catch {
          degraded = true;
        }

        // Some privacy/quota implementations reject removeItem while still
        // permitting a small write. Persist an empty value as a best-effort
        // logout backstop. The tombstone remains the in-tab source of truth.
        try {
          resolvedBacking.setItem(key, "");
          authoritative.delete(key);
          return true;
        } catch {
          degraded = true;
        }
      }
      return false;
    },

    isDegraded() {
      return degraded;
    },
  };
}

/**
 * Persist a complete reload-safe session through the resilient adapter. Both
 * keys are attempted so the adapter's shadow/tombstone state remains coherent,
 * but success means every required backing-store operation succeeded.
 *
 * @param {ReturnType<typeof createAuthStorage>} storage
 * @param {string} token
 * @param {string} [expiresAt]
 */
function persistAuthSession(storage, token, expiresAt = "") {
  const tokenPersisted = storage.setItem(TOKEN_STORAGE_KEY, token);
  const expiryPersisted = expiresAt
    ? storage.setItem(TOKEN_EXPIRES_AT_STORAGE_KEY, expiresAt)
    : storage.removeItem(TOKEN_EXPIRES_AT_STORAGE_KEY);
  if (tokenPersisted && expiryPersisted) {
    return true;
  }

  // A partially persisted session is unsafe: a reload could otherwise revive
  // an older token or pair a new token with stale expiry metadata. Tombstone
  // both keys in this adapter and independently attempt to remove both durable
  // values before reporting failure. removeItem retains an authoritative
  // in-memory tombstone even when the backing store rejects cleanup.
  storage.removeItem(TOKEN_STORAGE_KEY);
  storage.removeItem(TOKEN_EXPIRES_AT_STORAGE_KEY);
  return false;
}

export { createAuthStorage, persistAuthSession };
