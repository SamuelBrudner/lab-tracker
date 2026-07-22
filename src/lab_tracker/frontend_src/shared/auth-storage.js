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

const DEFAULT_BACKING = Symbol("default-auth-backing");
const TOMBSTONE = Symbol("auth-storage-tombstone");

function createAuthStorage(backing = DEFAULT_BACKING) {
  const shadow = new Map();
  const authoritative = new Set();
  let degraded = false;
  let resolvedBacking = backing;

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
  }
  if (!resolvedBacking) {
    degraded = true;
  }

  function shadowValue(key) {
    if (!shadow.has(key) || shadow.get(key) === TOMBSTONE) {
      return "";
    }
    return shadow.get(key);
  }

  return {
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

    setItem(key, value) {
      const normalized = String(value);
      shadow.set(key, normalized);
      if (resolvedBacking) {
        try {
          resolvedBacking.setItem(key, normalized);
          authoritative.delete(key);
          return;
        } catch {
          degraded = true;
        }
      }
      authoritative.add(key);
      degraded = true;
    },

    removeItem(key) {
      shadow.set(key, TOMBSTONE);
      authoritative.add(key);
      if (resolvedBacking) {
        try {
          resolvedBacking.removeItem(key);
          authoritative.delete(key);
          return;
        } catch {
          degraded = true;
        }

        // Some privacy/quota implementations reject removeItem while still
        // permitting a small write. Persist an empty value as a best-effort
        // logout backstop. The tombstone remains the in-tab source of truth.
        try {
          resolvedBacking.setItem(key, "");
          authoritative.delete(key);
          return;
        } catch {
          degraded = true;
        }
      }
    },

    isDegraded() {
      return degraded;
    },
  };
}

export { createAuthStorage };
